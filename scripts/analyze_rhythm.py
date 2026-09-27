#!/usr/bin/env python3
"""analyze_rhythm.py - 视频节奏分析（2026-09-28 新增）

「BGM 节奏必须与视频节奏一致、要有高潮起落」——这句话要能落地，前提是把
"视频节奏"变成一个**可计算**的量。本脚本就是那个量。

  输入：视频文件
  输出：work/rhythm.json

它回答三个问题：
  1. 视频**什么时候**最激烈？  -> climax / climaxes / motion 曲线
  2. 整条片子的强度怎么走？     -> intensity_profile（rising/falling/wave/flat）
  3. 配乐该用多快的 BPM？       -> suggested_bpm / beat_grid_sec

⚠️ 为什么需要「运动强度」而不只是「剪辑点」：
   2026-09-28 实测 01（奥尼尔·艾佛森 50s 单镜头）场景切换检出 **0 个** ——
   慢剪辑 / 长镜头素材只靠 scene detect 会得到空结果，BGM 也就无从匹配。
   运动强度（逐帧像素差）对这类素材才是有效信号。**两个指标必须一起看。**

实现（无新依赖：conda python 的 numpy + scipy；librosa 本机未装，故全部手写）：
  * 场景切换：ffmpeg `select='gt(scene,T)'` + showinfo 解析 pts_time
  * 运动强度：ffmpeg 抽低分辨率灰帧 -> numpy 逐帧差 -> 归一化
  * 高潮点  ：scipy.signal.find_peaks 在平滑后的运动强度曲线上取峰
  * BPM     ：运动强度曲线自相关求主周期（主）+ 镜头时长中位数印证（辅）
"""

import argparse
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import run, save_json, make_result, log, work_path, require_tools  # noqa: E402
from video_probe import probe_video  # noqa: E402

# 抽帧几何：宽 160、高按源比例折算（都是偶数，保证 rawvideo 行对齐）
SAMPLE_W = 160
MIN_SAMPLE_H = 64

# 抽帧帧率随片长自适应，避免长片把内存和时间吃满
FPS_TIERS = [(120, 12), (300, 8), (float("inf"), 6)]

# 运动强度曲线平滑窗口（秒）
SMOOTH_SEC = 0.8
# 高潮点之间的最小间隔（秒）——避免把一次连续冲框判成十几个峰
PEAK_MIN_DISTANCE_SEC = 1.5


# ============================================================
# 场景切换
# ============================================================
def detect_cuts(video_path, threshold=0.3):
    """场景切换时刻（秒）。失败时返回空列表并给出 warning。

    ffmpeg 的 scene 分数输出到 stderr 的 showinfo 行里，形如
        [Parsed_showinfo_1 @ ...] n:0 pts:0 pts_time:1.234 ...

    ⚠️ 日志级别必须是 `info`（showinfo 走 info 级）。2026-09-28 踩过：
       用 `-v error` 跑出 **0 个切换点**，误判成"这条片子是单镜头"，
       实际是日志级别把 showinfo 输出一起吞了。
    """
    r = run(
        ["ffmpeg", "-v", "info", "-i", video_path,
         "-vf", f"select='gt(scene,{threshold})',showinfo",
         "-an", "-f", "null", "-"],
        check=False,
    )
    text = (r.stderr or "") + (r.stdout or "")
    cuts = []
    for line in text.splitlines():
        if "pts_time:" not in line:
            continue
        for tok in line.split():
            if tok.startswith("pts_time:"):
                try:
                    cuts.append(round(float(tok.split(":", 1)[1]), 3))
                except ValueError:
                    pass
    return sorted(set(cuts))


def merge_close_cuts(cuts, min_gap=0.3):
    """合并间隔过近的切换点。

    同一处画面闪变会被 scene 检测连报好几次（实测 21.500 / 21.538 只差 0.038s），
    不合并会把「镜头时长中位数」算小、BPM 算高。
    """
    out = []
    for t in cuts:
        if not out or t - out[-1] >= min_gap:
            out.append(t)
    return out


# ============================================================
# 运动强度
# ============================================================
def sample_gray_frames(video_path, fps, w, h):
    """抽灰色原始帧 -> ndarray(n, h, w) uint8。

    直接用 subprocess 拿二进制 stdout（common.run 是 text=True，不能用于 rawvideo）。
    """
    cmd = [
        "ffmpeg", "-v", "error", "-i", video_path,
        "-an",
        "-vf", f"fps={fps},scale={w}:{h},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"frame sampling failed: {r.stderr.decode('utf-8', 'ignore')[-400:]}")
    buf = np.frombuffer(r.stdout, dtype=np.uint8)
    per = w * h
    n = len(buf) // per
    if n < 2:
        return None
    return buf[: n * per].reshape(n, h, w)


def motion_curve(frames, fps, smooth_sec=SMOOTH_SEC):
    """逐帧像素差 -> 逐帧运动强度（0-1）。"""
    f = frames.astype(np.float32)
    diff = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))       # (n-1,)
    # 用 95 分位归一化：单次剧烈闪变（闪白/转场）不该把整条曲线压扁
    denom = np.percentile(diff, 95)
    if denom <= 1e-6:
        denom = max(float(diff.max()), 1e-6)
    m = np.clip(diff / denom, 0.0, 1.0)

    # 平滑（时间维移动平均）
    win = max(1, int(round(smooth_sec * fps)))
    if win > 1 and len(m) >= win:
        k = np.ones(win, dtype=np.float32) / win
        m = np.convolve(m, k, mode="same")

    times = (np.arange(len(m), dtype=np.float32) + 1.0) / float(fps)
    return times, m


# ============================================================
# BPM 推导
# ============================================================
def bpm_from_autocorr(times, motion, fps):
    """运动强度曲线自相关 -> 主周期 -> BPM。

    集锦片的运动强度是准周期信号（一波攻防一个起伏），主周期即"视频的拍子"
    的近似。只信任 [0.30s, 2.0s]（30-200 BPM）区间，再用倍频折算到 [70,170]。
    """
    x = motion - float(np.mean(motion))
    if len(x) < int(fps * 2) or not np.any(x):
        return None
    ac = np.correlate(x, x, mode="full")[len(x) - 1:]
    if ac[0] <= 0:
        return None
    ac = ac / ac[0]

    lo = max(1, int(round(0.30 * fps)))
    hi = min(len(ac) - 1, int(round(2.00 * fps)))
    if hi <= lo:
        return None
    seg = ac[lo:hi + 1]
    lag = lo + int(np.argmax(seg))
    if lag <= 0:
        return None

    bpm = 60.0 * fps / lag
    # 倍频修正到常用配乐区间
    while bpm < 70:
        bpm *= 2
    while bpm > 170:
        bpm /= 2
    return round(float(bpm), 1), round(float(ac[lag]), 3)


def bpm_from_cuts(cuts, preferred=(110.0, 165.0), max_beats=6):
    """镜头时长中位数 -> 使「每个镜头 ≈ 整数拍」且落在 preferred 区间的 BPM。

    为什么以**剪辑点**而不是画面运动自相关为主：
      BGM 真正要"卡"的是**剪辑点**（观众看到的是切镜与鼓点同时发生）。
      画面运动的自相关反映的是"攻防起伏"，周期常常好几秒，会给出一条
      偏低的 BPM（实测 01 给 90），不符合"节奏要明显"的诉求。
      按镜头时长折算整数拍，才能得到 140+ 的密集鼓点。

    实测 01：镜头中位数 1.654s -> 每镜头 4 拍 -> 144.9 BPM。
    """
    if len(cuts) < 4:
        return None
    spans = np.diff(cuts)
    med = float(np.median(spans))
    if med <= 0.05:
        return None
    base = 60.0 / med          # 每镜头 1 拍时的 BPM

    def collect(lo, hi):
        out = []
        for n in range(1, max_beats + 1):
            bpm = base * n
            if lo <= bpm <= hi:
                out.append((n, round(bpm, 1)))
        return out

    cands = collect(*preferred)
    if not cands:              # 放宽一次
        cands = collect(max(70.0, preferred[0] * 0.7), min(185.0, preferred[1] * 1.2))
    if not cands:
        return None
    # 取拍数最多的候选：拍数越多＝每镜头被切得越细＝节奏感越强
    n, bpm = cands[-1]
    return {"bpm": bpm, "beats_per_shot": n, "shot_median_sec": round(med, 3)}


def peak_times(times, motion, fps):
    """运动强度局部峰值 -> 高潮候选（按强度降序）。"""
    try:
        from scipy.signal import find_peaks
    except ImportError:
        return []
    dist = max(1, int(round(PEAK_MIN_DISTANCE_SEC * fps)))
    idx, props = find_peaks(motion, distance=dist, height=0.45 * float(motion.max()))
    order = np.argsort(props["peak_heights"])[::-1]
    out = []
    for i in order:
        p = int(idx[i])
        out.append({"t": round(float(times[p]), 2),
                    "strength": round(float(motion[p]), 3)})
    return out


def intensity_profile(motion):
    """rising / falling / wave / flat。"""
    n = len(motion)
    if n < 6:
        return "flat"
    seg = max(1, n // 3)
    head = float(np.mean(motion[:seg]))
    tail = float(np.mean(motion[-seg:]))
    body = float(np.mean(motion))
    std = float(np.std(motion))
    if body <= 1e-6:
        return "flat"
    if tail > head * 1.25 and tail > body:
        return "rising"
    if head > tail * 1.25 and head > body:
        return "falling"
    if std / body > 0.35:
        return "wave"
    return "flat"


# ============================================================
# 主流程
# ============================================================
def pick_fps(duration):
    for limit, fps in FPS_TIERS:
        if duration <= limit:
            return fps
    return FPS_TIERS[-1][1]


def analyze(video_path, scene_threshold=0.3, fps_override=None, width=SAMPLE_W):
    probe = probe_video(video_path) or {}
    duration = float(probe.get("duration_sec") or 0.0)
    src_w = int(probe.get("width") or 0)
    src_h = int(probe.get("height") or 0)
    warnings = []

    # 抽帧几何：保持源比例
    w = int(width)
    if src_w and src_h:
        h = int(round(w * src_h / float(src_w)))
    else:
        h = 120
    h = max(MIN_SAMPLE_H, h - (h % 2))
    w = w - (w % 2)

    fps = float(fps_override) if fps_override else float(pick_fps(duration))

    frames = sample_gray_frames(video_path, fps, w, h)
    if frames is None:
        return make_result(
            status="degraded", confidence=0.0, warnings=["视频太短或抽帧失败"],
            duration_sec=round(duration, 3), suggested_bpm=[120.0, 140.0],
            beat_grid_sec=0.46, bpm_source="default", motion=[], cuts=[],
        )

    times, motion = motion_curve(frames, fps)
    sampled = int(round(len(frames) / fps * 2))  # 0.5s 一格的展示用曲线

    raw_cuts = detect_cuts(video_path, scene_threshold) if duration >= 2 else []
    cuts = merge_close_cuts(raw_cuts)
    cut_rate = round(len(cuts) / duration * 60.0, 2) if duration > 0 else 0.0
    # 镜头时长中位数是客观测量，与 BPM 推导**解耦**：
    # 长镜头素材推不出 BPM，但这个数依然有参考价值（2026-09-28 05 长片教训练）
    shot_median = None
    if len(cuts) >= 4:
        shot_median = round(float(np.median(np.diff(cuts))), 3)

    peaks = peak_times(times, motion, fps)
    climax = peaks[0]["t"] if peaks else round(duration * 0.5, 2)
    # 中后段最强峰：BGM 的 drop 一般对齐这里；片头最强峰常是开场特效/黑场转场
    body = [p for p in peaks if p["t"] >= duration * 0.25]
    climax_mid = body[0]["t"] if body else climax

    ac = bpm_from_autocorr(times, motion, fps)
    ac_conf = ac[1] if ac else None
    cuts_bpm = bpm_from_cuts(cuts)

    bpm_source = "default"
    if cuts_bpm:
        bpm_mid = cuts_bpm["bpm"]
        bpm_lo, bpm_hi = round(bpm_mid * 0.94, 1), round(bpm_mid * 1.06, 1)
        bpm_source = "cuts"
        if ac and abs(ac[0] - bpm_mid) / bpm_mid <= 0.15:
            bpm_source = "cuts+motion"          # 两条独立信号互证
    elif ac and ac[1] >= 0.25 and ac[0] >= 100:
        bpm_mid = ac[0]
        bpm_lo, bpm_hi = round(bpm_mid * 0.92, 1), round(bpm_mid * 1.08, 1)
        bpm_source = "motion_autocorr"
    else:
        # 剪辑稀疏（长镜头）或画面自相关给出偏低值（<100）。
        # 此时**不要**直接把低 BPM 交出去 —— 用户明确要求"节奏要明显"，
        # 而这类素材恰恰需要密鼓点来提气。按 §10 取中高区间。
        bpm_mid, bpm_lo, bpm_hi = 132.0, 120.0, 145.0
        bpm_source = "default_energetic"
        warnings.append(
            f"剪辑稀疏（{len(cuts)} 处切换 / {duration:.0f}s，镜头中位数 "
            f"{shot_median}s），剪辑点不足以推 BPM；画面自相关给 "
            f"{(ac[0] if ac else 0.0):.0f} BPM 偏低 → 按「节奏要明显」取 120–145。"
            f"选曲时优先用 BPM 130–150 的密集鼓点曲目。"
        )

    return make_result(
        status="ok" if not warnings else "ok_with_warnings",
        confidence=round(min(0.9, (ac_conf or 0.3) + 0.3), 3),
        warnings=warnings,
        duration_sec=round(duration, 3),
        sample_fps=fps,
        sample_wh=[w, h],
        cuts=cuts,
        cut_count=len(cuts),
        raw_cut_count=len(raw_cuts),
        cut_rate_per_min=cut_rate,
        shot_len_median_sec=shot_median,
        beats_per_shot=(cuts_bpm["beats_per_shot"] if cuts_bpm else None),
        motion_sample_interval=round(1.0 / fps, 4),
        motion_times=[round(float(t), 2) for t in times[::max(1, len(times) // sampled)]],
        motion=[round(float(v), 3) for v in motion[::max(1, len(motion) // sampled)]],
        peaks=peaks[:12],
        climax=climax,
        climax_mid=climax_mid,
        intensity_profile=intensity_profile(motion),
        suggested_bpm=[bpm_lo, bpm_hi],
        bpm_mid=bpm_mid,
        beat_grid_sec=round(60.0 / bpm_mid, 3),
        bpm_source=bpm_source,
    )


def main():
    ap = argparse.ArgumentParser(description="视频节奏分析 -> rhythm.json")
    ap.add_argument("--video", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--out", help="输出路径（默认 <work>/rhythm.json）")
    ap.add_argument("--scene-threshold", type=float, default=0.3,
                    help="场景切换判定阈值（默认 0.3；慢剪辑片子可降到 0.2）")
    ap.add_argument("--fps", type=float, default=None,
                    help="抽帧率（默认按片长自适应 12/8/6）")
    ap.add_argument("--width", type=int, default=SAMPLE_W, help="抽帧宽度（默认 160）")
    args = ap.parse_args()

    require_tools("ffmpeg", "ffprobe")
    res = analyze(args.video, args.scene_threshold, args.fps, args.width)

    out = args.out or work_path(args.work_dir, "rhythm.json")
    save_json(out, res)
    log.info(
        "rhythm: %s | cuts=%d (%.1f/min) | climax=%.2fs | %s | BPM %s (%s)",
        os.path.basename(args.video), res.get("cut_count", 0),
        res.get("cut_rate_per_min", 0.0), res.get("climax", 0.0),
        res.get("intensity_profile", "?"), res.get("suggested_bpm"),
        res.get("bpm_source"),
    )
    print(out)


if __name__ == "__main__":
    main()
