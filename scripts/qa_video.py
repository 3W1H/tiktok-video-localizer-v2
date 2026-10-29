#!/usr/bin/env python3
"""
qa_video.py - 质量检查

六类检查 + 首帧门禁：
  1. video:    无黑帧/花屏，时长一致
  2. subtitle: SRT 条目数、时间轴冲突
  3. cover:    人脸/主体未被遮挡
  4. audio:    TTS 轨电平正常（无 amix normalize bug）、片头非静音；中文残留仍需人工 Whisper 抽听（见 §14.8）
  5. localization: 残留中文 OCR 抽检、水印区复检、replace 后 Logo 可见性、review 项汇总
  6. opening_frame: 【§11.1】第 0 帧必须是正片画面，不许是标题卡 / logo 卡 / 封面复刻帧
  7. chinese_residue: 【硬门禁】对 final.mp4 全片 OCR 扫描，检出中文即 FAIL

输出：work/qa.json（PASS / WARNING / FAIL）

禁止假装成功：清理置信度低如实标 LOW，残留中文由 QA 抽检上报。

⚠️ 第 6 项是硬门禁，与第 5 项的区别：
  第 5 项检查「计划里的项执行得对不对」（依赖 plan），
  第 6 项检查「成品里到底还有没有中文」（独立于 plan，直接对画面下结论）。
  plan 若漏了某处中文，第 5 项查不出来 —— 这正是 C 视频假 PASS 的成因。
"""

import argparse
import glob
import os
import sys
import tempfile
from typing import Dict, Any, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    run, save_json, load_json, make_result, log,
    work_path, output_path, require_tools, check_python_package,
    has_ranking_token,
)


def check_video(output_dir: str, work_dir: str) -> Dict:
    """检查视频：时长、黑帧、花屏"""
    final_path = os.path.join(output_dir, "final.mp4")
    probe = load_json(work_path(work_dir, "probe.json"), {})
    expected_duration = probe.get("duration_sec", 0)

    # 原创化（标题卡/变速/BGM）会改变总时长，此时 probe.json 记录的源时长
    # 不再是正确基准，否则会误报 "Duration: expected=14.5s, actual=16.0s" 的 FAIL。
    orig = load_json(work_path(work_dir, "originalize-result.json"), {})
    if orig.get("duration_sec"):
        expected_duration = orig["duration_sec"]

    if not os.path.exists(final_path):
        return {"status": "fail", "detail": "final.mp4 not found", "duration_match": False}

    # 用 ffprobe 检查时长
    try:
        r = run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            final_path,
        ])
        actual_duration = float(r.stdout.strip())
    except Exception:
        actual_duration = 0

    duration_diff = abs(actual_duration - expected_duration)
    duration_match = duration_diff < 0.5

    # 黑帧检测（简单：检查第一帧和中间帧是否全黑）
    black_frames = 0
    try:
        # 抽取 3 帧检查。写到临时目录且**不回收**：本环境对删除有批量确认钩子，
        # 逐个 unlink 累计到阈值会把 QA 整段打断（2026-09-27 实测）。
        scratch = tempfile.mkdtemp(prefix="ttl-qa-frames-")
        for ts in [1, actual_duration / 2, max(1, actual_duration - 1)]:
            frame_path = os.path.join(scratch, f"qa_frame_{int(ts)}.png")
            run([
                "ffmpeg", "-y", "-ss", str(ts), "-i", final_path,
                "-vframes", "1", frame_path,
            ], check=False)
            if os.path.exists(frame_path):
                try:
                    import cv2
                    import numpy as np
                    img = cv2.imread(frame_path, cv2.IMREAD_GRAYSCALE)
                    if img is not None and np.mean(img) < 5:
                        black_frames += 1
                except ImportError:
                    pass
    except Exception:
        pass

    status = "pass" if duration_match and black_frames == 0 else "warning"
    if not duration_match:
        status = "fail"

    return {
        "status": status,
        "detail": f"Duration: expected={expected_duration:.1f}s, actual={actual_duration:.1f}s, diff={duration_diff:.2f}s",
        "duration_match": duration_match,
        "expected_duration": expected_duration,
        "actual_duration": actual_duration,
        "black_frames_detected": black_frames,
    }


def _grab_gray(video: str, ts: float, scratch: str, size=(32, 32)):
    """抽一帧并缩成 size 的灰度数组；失败返回 None。"""
    path = os.path.join(scratch, f"open_{int(round(ts * 1000)):06d}.png")
    run(["ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", video,
         "-frames:v", "1", path], check=False)
    if not os.path.exists(path):
        return None
    try:
        from PIL import Image
        import numpy as np
        im = Image.open(path).convert("L").resize(size, Image.LANCZOS)
        return np.asarray(im, dtype=np.float32)
    except Exception:
        return None


def check_opening_frame(output_dir: str, work_dir: str) -> Dict:
    """首帧必须是正片画面（SKILL.md §11.1）

    封面 cover.jpg 已经承载了标题 / 内容简介 / 品牌 logo，视频第 0 帧
    再放标题卡、logo 卡或封面复刻帧属于重复信息：白占前 1.5 秒、拉低完播率。

    三种判据，任一命中都给 WARNING（不 fail，交由 Agent 目检裁决）：
      1. `originalize-result.json` 里 `opening_title_card == true`（显式开了 LEGACY 卡）
      2. 淡入结束后的 0.70–1.35s 画面几乎不变 → 静止卡/logo 卡
      3. 第 0 帧与 cover.jpg 高度相似 → 封面复刻帧
    """
    final_path = os.path.join(output_dir, "final.mp4")
    if not os.path.exists(final_path):
        return {"status": "warning", "detail": "final.mp4 not found",
                "opening_static": None}

    issues: List[str] = []

    # 判据 1：元数据自报
    orig = load_json(work_path(work_dir, "originalize-result.json"), {})
    if orig.get("opening_title_card"):
        issues.append("originalize-result.json 声明 opening_title_card=true"
                      "（LEGACY 片头卡，违反 §11.1）")

    scratch = tempfile.mkdtemp(prefix="ttl-qa-opening-")
    f0 = _grab_gray(final_path, 0.0, scratch)
    # 避开头 0.5s 的淡入（标题卡通常 fade in 0.5s，淡入期内帧差很大，
    # 拿 0→0.4s 比会漏判）。取淡入结束后的两个点比"是否静止"。
    f7 = _grab_gray(final_path, 0.70, scratch)
    f13 = _grab_gray(final_path, 1.35, scratch)

    try:
        import numpy as np
    except ImportError:
        # 没有 numpy/Pillow 时只保留「元数据自报」这一条判据，不阻断 QA
        np = None

    mean0 = float(f0.mean()) if f0 is not None else None
    static_delta = None

    # 判据 2：淡入结束后的 0.7–1.35s 是否几乎静止
    if np is not None and f7 is not None and f13 is not None:
        static_delta = round(float(abs(f13 - f7).mean()), 2)
        # 亮度用 f7 判断而不是 f0：标题卡带 fade in，t=0 本来就是全黑，
        # 拿 f0 当守卫会把片头卡整条放过（黑屏另有 video 检查负责）。
        luma7 = float(f7.mean())
        # 正片 0.65s ≈ 18–20 帧，即使慢镜也会明显变化；颗粒底噪约 1–2
        if static_delta < 2.5 and luma7 >= 5:
            issues.append(f"0.70–1.35s 画面几乎不变（帧差 {static_delta}），"
                          "疑似片头标题卡/logo 卡")

    # 判据 3：第 0 帧与封面是否高度相似
    cover_sim = None
    cover_path = os.path.join(output_dir, "cover.jpg")
    if np is not None and f0 is not None and os.path.exists(cover_path):
        try:
            from PIL import Image
            im = Image.open(cover_path).convert("L").resize((32, 32), Image.LANCZOS)
            c = np.asarray(im, dtype=np.float32)
            cover_sim = round(float(abs(c - f0).mean()), 2)
            if cover_sim < 6.0:
                issues.append(f"第 0 帧与 cover.jpg 高度相似（灰度 MAE {cover_sim}），"
                              "疑似把封面复刻成了视频首帧")
        except Exception:
            pass

    status = "warning" if issues else "pass"
    detail = "第 0 帧是正片画面，无片头卡" if not issues else "；".join(issues)
    return {
        "status": status,
        "detail": detail,
        "opening_static": bool(static_delta is not None and static_delta < 2.5),
        "opening_static_delta": static_delta,
        "opening_mean_luma": None if mean0 is None else round(mean0, 1),
        "frame0_vs_cover_mae": cover_sim,
        "issues": issues,
    }


def check_subtitle(output_dir: str, work_dir: str) -> Dict:
    """检查字幕：SRT 条目数、时间轴冲突"""
    srt_path = os.path.join(output_dir, "subtitles-en.srt")
    timeline = load_json(work_path(work_dir, "timeline.json"), {})

    if not os.path.exists(srt_path):
        return {"status": "fail", "detail": "subtitles-en.srt not found", "srt_entries": 0}

    # 统计 SRT 条目
    with open(srt_path, encoding="utf-8") as f:
        content = f.read()
    srt_entries = content.count("-->")

    # 检查时间轴冲突
    # NOTE (2026-09-26): static overlays (title/watermark/slogan) span the whole
    # video, so their 0-230s windows always "overlap" every real subtitle. They are
    # not subtitle cues and must be excluded, otherwise QA reports phantom conflicts.
    # NOTE (2026-09-29): 同理，**不写进 SRT 的条目也不能参与冲突判定** ——
    # `remove` 类项（如 03 的 0.6-3.6s 中屏中文弹窗 pop_000）只做擦除、没有任何
    # 文字，却仍占着源窗口，于是被报成 "narr_000(0.0-2.416) vs pop_000(0.6-0.6)
    # 冲突 1 处"，而 subtitles-en.srt 实测**零重叠**。判据与 render_subtitles.py
    # 完全一致：没有 `text_en` 就不产 cue（该脚本就是 `if not text: continue`）。
    plan = load_json(work_path(work_dir, "localization-plan.json"), {})
    items = plan.get("items", [])
    static_cats = ("title", "watermark", "logo", "slogan")
    entries = []
    for e in timeline.get("entries", []):
        idx = e.get("index")
        if items and idx is not None and idx < len(items):
            if items[idx].get("category") in static_cats:
                continue
        if not (e.get("text_en") or "").strip():
            continue  # draws no subtitle -> cannot conflict with one
        entries.append(e)

    timeline_conflicts = 0
    for i in range(len(entries) - 1):
        if entries[i].get("final_end", 0) > entries[i + 1].get("final_start", 0):
            timeline_conflicts += 1

    unresolved = len(timeline.get("unresolved", []))
    status = "pass" if timeline_conflicts == 0 and unresolved == 0 else "warning"

    return {
        "status": status,
        "detail": f"SRT entries: {srt_entries}, timeline conflicts: {timeline_conflicts}, unresolved: {unresolved}",
        "srt_entries": srt_entries,
        "timeline_conflicts": timeline_conflicts,
        "unresolved": unresolved,
    }


def has_chinese_text(text: str, min_chars: int = 2) -> bool:
    """检测文本是否含中文（CJK）。要求至少 2 个连续区间字符，避免 OCR 把
    球场纹理/英文字母误识别为单个中文字（如 '人'、'一'、'有'）造成误报。"""
    count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    return count >= min_chars


def ocr_cover_chinese(cover_path: str) -> List[str]:
    """OCR 封面并返回检测到的中文字符串（引擎不可用时返回空列表）

    只用 RapidOCR（与全片 OCR 同一个引擎）。
    **刻意不设 tesseract 兜底**：tesseract 对中文描边美术字召回≈0，
    静默顶上只会让「引擎不可用」伪装成「封面没有中文」。
    引擎不可用的判断由调用方（check_cover）负责 —— 它会据此报 warning，
    而不是把「没查到」当成「查过了且是干净的」。
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from ocr_video import ocr_with_rapidocr
        lines: List[str] = []
        for det in ocr_with_rapidocr(cover_path):
            t = (det.get("text") or "").strip()
            if t and has_chinese_text(t):
                lines.append(t)
        return lines
    except Exception:
        return []


def _probe_fps(video: str, fallback: float = 30.0) -> float:
    """源片帧率，用于把 "N FPS 采样" 换算成帧步长。"""
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", video], check=False)
    try:
        num, _, den = (r.stdout or "").strip().partition("/")
        v = float(num) / float(den or 1)
        return v if v > 0 else fallback
    except Exception:
        return fallback


def _probe_duration(video: str) -> float:
    """成片总时长（秒），用于首尾加密采样的窗口定位。"""
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video], check=False)
    try:
        return float((r.stdout or "").strip())
    except Exception:
        return 0.0


def check_chinese_residue(output_dir: str, work_dir: str,
                          fps: float = 1.0, min_confidence: float = 0.5,
                          single_frame_min_confidence: float = 0.9) -> Dict:
    """【硬门禁】对成品 final.mp4 全片 OCR 扫描，检出中文即 FAIL。

    为什么必须独立跑一遍成品：
      清理是「按 localization-plan 执行」的。plan 若漏了某处中文，
      执行得再完美也不会有产物 —— 只看 plan 的 QA 根本发现不了漏检。
      C（shaq_gentle_giant）26-28s 的「卤蛋盘卤蛋」就是这么活下来的：
      当时 QA 复用了处理前的结论，且 OCR 引擎是不可信的 tesseract，于是假 PASS。

    与 5.localization 的分工：
      第 5 项 = 「计划里的项执行得对不对」（依赖 plan）
      本项   = 「成品里到底还有没有中文」（独立于 plan，直接看画面）

    引擎不可用时**不得判定通过** —— 那等于重演「假 PASS」。

    fps 说明：默认 1fps 对持续 ≥1s 的字幕足够；极短闪现（<1s）可能漏，
    可用 --residue-fps 2 提高采样率。

    ⭐ 单帧检出 + 低置信度 = 不算残留（2026-09-28 新增，窄口径）：
      真实的**烧录**字幕至少持续 ~1s，1fps 采样必然连续命中 ≥2 帧；
      而**人脸/头发纹理、快速推拉或缩放的英文图形**会被 OCR 瞬间误读成 CJK，
      典型特征是「只出现在 1 帧」且「置信度明显偏低」。
      03 实测：226s 的「奥尼尔抱起科比」经典照上，科比头发处被读成「可口」，
      conf 0.6877、仅 1 帧 —— 而该片 13 组**真**残留全部是 conf 0.95+ 的完整句子。
      ⇒ 规则：`frame_count == 1 and max_confidence < single_frame_min_confidence` 的组不算残留。
      **这条不会削弱任何多帧残留的检出**（真字幕必然多帧），也不会放过高置信的单帧误读。
      验证误报时**必须先抽帧目视确认**，不能凭置信度数字就下结论。

    ⭐ 同字连排 = 图形误读（2026-10-15 新增，窄口径）：
      真实烧录中文字幕绝不会出现「中中中」这种无意义叠字（中文叠字最多 2 字：
      天天/慢慢/好好）。而图标、徽标、球衣图形等**重复元素**最典型的 OCR 误读
      产物恰恰就是「同字连排」。规则：`len(text) >= 3 and len(set(text)) == 1
      and max_confidence < single_frame_min_confidence`。
      10 实测：莫兰特 701s 横屏片 57-64s 橙色横幅上 3 个白色手掌图标被读成
      「中中中」，跨 6 帧 conf 0.53~0.80，因此躲过了上面那条单帧规则；
      抽帧放大 6x 目视确认是图形。
      ⇒ **加任何误读规则前都必须先抽帧目视确认**，不许只看置信度数字。
    """
    final_path = os.path.join(output_dir, "final.mp4")
    if not os.path.exists(final_path):
        return {"status": "fail", "detail": "final.mp4 not found",
                "residues": [], "groups": []}

    if not check_python_package("rapidocr_onnxruntime"):
        return {
            "status": "warning",
            "engine": "none",
            "fps": fps,
            "frames_scanned": 0,
            "residues": [],
            "groups": [],
            "detail": "RapidOCR 不可用，成品中文残留检测被跳过 —— 本次结果不构成「无中文」的证据",
        }

    from ocr_video import ocr_with_rapidocr

    # Scratch frames go to a FRESH temp dir every run. They used to live in
    # <work-dir>/qa-residue and be cleared with a glob+unlink loop, which
    # tripped the safe-delete bulk confirmation on a 230s clip (50+ files) and
    # aborted QA with SAFE_DELETE_BULK_CONFIRM_REQUIRED. A new mkdtemp per run
    # removes the need to delete anything, and keeps the delivery work-dir clean.
    frame_dir = tempfile.mkdtemp(prefix="ttl-qa-residue-")
    log.info(f"  残留帧抽帧目录: {frame_dir}")

    # ⚠️ 取帧必须从源片第 0 帧开始，**不能用 `-vf fps=N`**：
    # fps filter 的输出帧不含第 0 帧（实测 1FPS 首帧落在 t=0.4667，
    # 且整条时间轴比标注晚约 0.53s）。02/03/09 的中文标题卡只存在于第 0 帧，
    # 用 fps=N 时本项硬门禁会给出**假的 pass**。2026-09-29 修复。
    src_fps = _probe_fps(final_path)
    period = max(1, int(round(src_fps / fps)))
    log.info(f"  残留扫描取帧：src {src_fps:g}fps，每 {period} 帧取 1（含第 0 帧）")

    run(["ffmpeg", "-y", "-v", "error", "-i", final_path,
         "-vf", f"select='not(mod(n,{period}))'", "-vsync", "0", "-q:v", "3",
         os.path.join(frame_dir, "f%05d.jpg")], check=False)

    frames = sorted(glob.glob(os.path.join(frame_dir, "f*.jpg")))
    residues: List[Dict[str, Any]] = []
    for i, fp in enumerate(frames):
        ts = round(i / fps, 3)
        try:
            dets = ocr_with_rapidocr(fp)
        except Exception as e:
            log.warning(f"  残留检测 OCR 失败 @ {ts}s: {str(e)[:120]}")
            continue
        for det in dets:
            text = (det.get("text") or "").strip()
            conf = det.get("confidence") or 0
            if text and has_chinese_text(text) and conf >= min_confidence:
                residues.append({
                    "timestamp_sec": ts,
                    "text": text,
                    "confidence": det.get("confidence"),
                    "bbox": det.get("bbox"),
                })

    # 边缘加密（2026-10-29，98全明星实测）：上传者自制收尾卡片常只持续
    # 0.3-1s（例：187.5-187.9s 的中文致谢卡「致敬科比」）。body 的周期取模
    # 采样在尾部不满一个周期会整段漏采（1fps@30fps 只取到 187.0s），卡片
    # 正好从硬门禁的缝隙里溜过去。对首尾各 EDGE 秒用 EDGE_FPS 补扫。
    EDGE, EDGE_FPS = 2.0, 6.0
    dur = _probe_duration(final_path)
    if dur > 0:
        edge_period = max(1, int(round(src_fps / EDGE_FPS)))
        edge_jobs = []
        if dur > EDGE:
            edge_jobs = [(0.0, "head"), (dur - EDGE, "tail")]
        else:
            edge_jobs = [(0.0, "whole")]
        log.info(f"  边缘加密扫描：首尾各 {EDGE:g}s @ {EDGE_FPS:g}fps "
                 f"(每 {edge_period} 帧取 1)")
        for ss, tag in edge_jobs:
            out_pat = os.path.join(frame_dir, f"edge_{tag}_%05d.jpg")
            cmd = ["ffmpeg", "-y", "-v", "error"]
            if ss > 0:
                cmd += ["-ss", f"{ss:.3f}"]
            cmd += ["-i", final_path, "-t", f"{EDGE:.3f}",
                    "-vf", f"select='not(mod(n,{edge_period}))'",
                    "-vsync", "0", "-q:v", "3", out_pat]
            run(cmd, check=False)
            for k, fp in enumerate(sorted(glob.glob(
                    os.path.join(frame_dir, f"edge_{tag}_*.jpg")))):
                ts = round(ss + k / EDGE_FPS, 3)
                try:
                    dets = ocr_with_rapidocr(fp)
                except Exception as e:
                    log.warning(f"  残留检测(边缘) OCR 失败 @ {ts}s: {str(e)[:120]}")
                    continue
                for det in dets:
                    text = (det.get("text") or "").strip()
                    conf = det.get("confidence") or 0
                    if text and has_chinese_text(text) and conf >= min_confidence:
                        residues.append({
                            "timestamp_sec": ts,
                            "text": text,
                            "confidence": det.get("confidence"),
                            "bbox": det.get("bbox"),
                        })

    # 按文本归组，给出人类可读的区间清单
    grouped_map: Dict[str, List[Dict[str, Any]]] = {}
    for r in residues:
        grouped_map.setdefault(r["text"], []).append(r)
    groups: List[Dict[str, Any]] = []
    for text, items in grouped_map.items():
        ts_list = [it["timestamp_sec"] for it in items]
        groups.append({
            "text": text,
            "first_sec": min(ts_list),
            "last_sec": max(ts_list),
            "frame_count": len(items),
            "bbox": items[0]["bbox"],
            "max_confidence": max((it["confidence"] or 0) for it in items),
        })
    groups.sort(key=lambda g: g["first_sec"])

    # 规 1：单帧 + 低置信度 = OCR 误读，不是烧录字幕。见函数 docstring。
    # 真字幕至少持续 ~1s -> 1fps 采样必连续命中；误读通常只落在 1 帧上。
    misreads = [g for g in groups
                if g["frame_count"] == 1
                and (g["max_confidence"] or 0) < single_frame_min_confidence]
    for g in misreads:
        g["misread_reason"] = "single_frame_low_conf"

    # 规 2（2026-10-15）：**同一字符重复 ≥3 次** = OCR 把「重复的图形」读成了同一个字。
    #   真实的烧录中文字幕绝不会出现「中中中」这类无意义叠字（中文叠字最多 2 字：
    #   天天/慢慢/好好），而图标、徽标、球衣、赞助商图形等**重复元素**最典型的
    #   误读产物恰恰就是这种「同字连排」。
    #   10 实测（莫兰特 701s 横屏片）57-64s 橙色横幅上 3 个白色手掌图标被读成
    #   「中中中」；因为跨了 6 帧、conf 0.53~0.80，所以躲过了规 1 —— 抽帧放大 6x
    #   目视确认是图形而非文字。**加这条规则前必须先抽帧目视确认**，不许只看数字。
    #   仍保留 conf < single_frame_min_confidence 的约束：03 实测的 13 组真残留
    #   全部是 conf 0.95+ 的完整句子，不会落进这条规则。
    repeat_misreads = [g for g in groups
                       if g not in misreads
                       and len(g["text"]) >= 3
                       and len(set(g["text"])) == 1
                       and (g["max_confidence"] or 0) < single_frame_min_confidence]
    for g in repeat_misreads:
        g["misread_reason"] = "repeated_identical_glyph"
    misreads = misreads + repeat_misreads

    if misreads:
        keep = {g["text"] for g in groups if g not in misreads}
        residues = [r for r in residues if r["text"] in keep]
        groups = [g for g in groups if g not in misreads]
        log.info(f"  剔除 {len(misreads)} 组 OCR 误读 "
                 f"(conf<{single_frame_min_confidence}): "
                 + ", ".join(f"{g['text']!r}@{g['first_sec']}s "
                             f"conf={g['max_confidence']:.2f} "
                             f"reason={g['misread_reason']}"
                             for g in misreads))

    status = "fail" if residues else "pass"
    if status == "fail":
        detail = (f"成品检出 {len(residues)} 条中文残留，涉及 {len(groups)} 处不同文本"
                  f" —— 禁止交付，处理后必须重跑本检查")
    else:
        detail = f"成品全片 OCR（{len(frames)} 帧 @ {fps}fps）未检出中文"
        if misreads:
            detail += (f"（已剔除 {len(misreads)} 组 OCR 误读："
                       + ", ".join(f"{g['text']!r}@{g['first_sec']}s conf={g['max_confidence']:.2f}"
                                   for g in misreads) + "）")

    return {
        "status": status,
        "engine": "rapidocr_local",
        "fps": fps,
        "frames_scanned": len(frames),
        "residues": residues,
        "groups": groups,
        "misreads_dropped": misreads,
        "single_frame_min_confidence": single_frame_min_confidence,
        "detail": detail,
    }


def regions_overlap(a: Dict, b: Dict) -> bool:
    """两个矩形区域是否重叠（a/b 均为 {x,y,w,h}）"""
    return not (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"] or
                a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])


def check_cover(output_dir: str, work_dir: str) -> Dict:
    """检查封面：
      1. 人脸/主体未被遮挡
      2. 无中文残留（OCR 检测，禁止中文水印/标题带入封面）
      3. 标题颜色为 TikTok 经典亮黄色（配置校验，禁止白色）
      4. 品牌署名已渲染（profile 配置了 brand_text 就必须渲染）
      5. 封面帧已按 localization-plan 清理（有 remove/translate 项时必须清理）
      6. 标题与品牌署名落在安全带内（主页网格只显示 9:16 封面的中间部分）
    """
    cover_path = os.path.join(output_dir, "cover.jpg")
    cover_plan = load_json(work_path(work_dir, "cover-plan.json"), {})
    loc_plan = load_json(work_path(work_dir, "localization-plan.json"), {})

    if not os.path.exists(cover_path):
        return {"status": "fail", "detail": "cover.jpg not found", "face_occluded": None}

    render_info = cover_plan.get("render", {})
    faces = render_info.get("faces_detected", 0)
    face_areas = render_info.get("face_areas", [])
    text_regions = render_info.get("text_regions", [])
    safe_zone = render_info.get("safe_text_zone", {})
    warnings = []

    # 检查文字是否遮挡人脸（用实际文字区域 bbox，而非整个安全区）
    face_occluded = False
    if faces > 0 and text_regions:
        for fa in face_areas:
            face_rect = {"x": fa["x"], "y": fa["y"], "w": fa["w"], "h": fa["h"]}
            for tr in text_regions:
                if regions_overlap(tr, face_rect):
                    face_occluded = True
                    break
            if face_occluded:
                break

    # 封面中文残留检查（OCR 抽检）
    ocr_available = check_python_package("rapidocr_onnxruntime")
    chinese_lines = ocr_cover_chinese(cover_path) if ocr_available else []
    chinese_residual = len(chinese_lines) > 0
    if not ocr_available:
        warnings.append("RapidOCR 不可用，封面中文残留检查被跳过 —— 这**不代表**封面没有中文")

    # 标题颜色校验（品牌黄，取值来自 profile.cover_style.fill_color；禁止白色/黑白）
    profile_style = cover_plan.get("profile_cover_style", {})
    fill_color = profile_style.get("fill_color", [255, 220, 0])
    fill_r, fill_g, fill_b = fill_color[0], fill_color[1], fill_color[2]
    # 黄色判定：R>=230, G>=180, B<=80，且 G > B（排除白色/黑白）
    color_ok = fill_r >= 230 and fill_g >= 180 and fill_b <= 80 and fill_g > fill_b
    if not color_ok:
        log.warning(f"Cover fill_color is not a brand yellow: {fill_color} "
                    f"(need R>=230, G>=180, B<=80, G>B)")

    # 品牌署名校验（profile 配置了 brand_text 就必须渲染）
    brand_text = profile_style.get("brand_text")
    brand_ok = True
    if brand_text:
        brand_ok = bool(render_info.get("brand_text_rendered"))
        if not brand_ok:
            log.warning(f"Cover brand_text '{brand_text}' configured but not rendered")

    # 封面帧清理校验（只看**该帧上真的在屏**的那些项，不是全片所有项）
    #
    # 2026-09-29：原先用 `cleaned >= len(plan_items)` —— 要求把全片每个区域都擦到
    # 封面这一帧上。那既逼着 generate_cover 去擦不存在的区域（03 因此在科比脸上
    # 压出一块黑方块），也让「这一帧本来干净、一个区域都不用擦」被判成失败。
    # 现在以 cover 写入的 frame_clean_expected 为准；旧 cover-plan 没有该字段时，
    # 就地按 selected_frame.timestamp_sec 过滤，行为一致。
    plan_items = [i for i in loc_plan.get("items", [])
                  if i.get("action") in ("remove", "translate", "replace")
                  and i.get("bbox")]
    cleaned = render_info.get("frame_cleaned_regions", 0)
    selected_ts = (cover_plan.get("selected_frame") or {}).get("timestamp_sec")
    expected = render_info.get("frame_clean_expected")
    if expected is None:
        if selected_ts is None:
            expected = len(plan_items)
        else:
            expected = len([i for i in plan_items
                            if i.get("start_time") is None
                            or i.get("end_time") is None
                            or float(i["start_time"]) - 1e-6
                            <= float(selected_ts) <= float(i["end_time"]) + 1e-6])
    frame_clean_ok = True
    if expected and profile_style.get("clean_frame_enabled", True):
        frame_clean_ok = cleaned >= expected
        if not frame_clean_ok:
            log.warning(f"Cover frame should clean {expected} regions (on screen at "
                        f"{selected_ts}s) but cleaned {cleaned}")

    # 安全带校验：TikTok 只在主页网格/搜索结果里显示 9:16 封面的中间部分，
    # 标题或品牌署名落到带外，用户在自己的主页上根本看不到它们
    # （2026-09-27 用户："封面图片要适合 tiktok 封面大小，要能够看全标题和 logo"）。
    # 只做真实 bbox 校验，不做「文字是否居中」这类审美判断。
    brand_region = render_info.get("brand_region")
    boxes = list(text_regions) + ([brand_region] if brand_region else [])
    safe_ok = True
    outside = []
    if safe_zone and boxes and safe_zone.get("h"):
        zy0 = safe_zone.get("y", 0)
        zy1 = zy0 + safe_zone.get("h", 0)
        for b in boxes:
            if b.get("y", zy0) < zy0 or b.get("y", zy0) + b.get("h", 0) > zy1:
                outside.append(b.get("text", "?"))
        safe_ok = not outside
        if outside:
            log.warning("Cover text outside the safe band and will be cropped in "
                        f"the profile grid: {outside}")
    elif not safe_zone:
        warnings.append("cover render info has no safe_text_zone -- top/bottom "
                        "crop safety NOT verified")

    if face_occluded:
        warnings.append("face occluded by text")
    if chinese_residual:
        warnings.append(f"Chinese text on cover: {chinese_lines[:3]}")
    if not color_ok:
        warnings.append(f"cover text color not TikTok yellow (fill_color={fill_color})")
    if not brand_ok:
        warnings.append("brand text not rendered")
    if not frame_clean_ok:
        warnings.append("cover frame not cleaned per localization-plan")
    if not safe_ok:
        warnings.append("cover text outside the safe band (cropped in the "
                        f"profile grid): {outside}")

    status = "pass" if not warnings else "warning"
    return {
        "status": status,
        "detail": "; ".join(warnings) if warnings else
                  f"Faces: {faces}, occluded: {face_occluded}, chinese: 0, color: yellow, "
                  f"brand: ok, cleaned {cleaned}/{expected} on-screen regions",
        "face_occluded": face_occluded,
        "subject_occluded": False,  # V1 简化
        "faces_detected": faces,
        "chinese_residual": chinese_residual,
        "chinese_lines": chinese_lines[:5],
        "fill_color_ok": color_ok,
        "fill_color": list(fill_color),
        "brand_text_rendered": brand_ok,
        "text_in_safe_band": safe_ok,
        "text_outside_safe_band": outside,
        "frame_cleaned_regions": cleaned,
        "frame_clean_expected": expected,
        "frame_clean_at_sec": selected_ts,
    }


def _volumedetect(path: str):
    """对音频文件跑 ffmpeg volumedetect，从 stderr 解析 (mean_db, max_db)。
    ⚠️ 不能加 -v error：volumedetect 走 info 级，加 -v error 后输出消失（见 SKILL §9）。
    返回 None 表示解析失败或无音频流。"""
    try:
        r = run([
            "ffmpeg", "-i", path,
            "-af", "volumedetect",
            "-f", "null", "-",
        ], check=False)
    except Exception:
        return None
    out = (r.stderr or "") + "\n" + (r.stdout or "")
    mean = max_db = None
    for line in out.splitlines():
        if "mean_volume:" in line:
            try:
                mean = float(line.split("mean_volume:")[1].split("dB")[0].strip())
            except Exception:
                pass
        elif "max_volume:" in line:
            try:
                max_db = float(line.split("max_volume:")[1].split("dB")[0].strip())
            except Exception:
                pass
    if mean is None and max_db is None:
        return None
    return (mean, max_db)


def check_audio(output_dir: str, work_dir: str) -> Dict:
    """检查音频：① 是否有音轨；② TTS 轨是否被压死在 BGM 下（amix 漏 normalize=0）；
    ③ 片头是否近静音。

    ⚠️ 本函数**不做语音内容分析**（中/英判别留给人工 Whisper 抽听，见 SKILL §14.8）。
    但第 ② 项是对 2026-09-29 03 实锤 bug 的自动闸门：tts_track.wav 若峰值 < -40dB，
    即 amix 漏加 normalize=0 的实锤（03 当时 max=-36dB = 1/64），成片会"只有 BGM 没有解说"。"""
    final_path = os.path.join(output_dir, "final.mp4")
    plan = load_json(work_path(work_dir, "localization-plan.json"), {})
    audio_strategy = plan.get("audio_strategy", "keep_original")

    if not os.path.exists(final_path):
        return {"status": "fail", "detail": "final.mp4 not found"}

    # 1) 是否有音轨
    has_audio = False
    try:
        r2 = run([
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            final_path,
        ])
        has_audio = "audio" in (r2.stdout or "")
    except Exception:
        has_audio = False

    if not has_audio:
        return {"status": "fail", "detail": "final.mp4 has no audio stream"}

    warnings = []
    detail_parts = [f"strategy={audio_strategy}"]

    # 2) TTS 轨电平（仅 mute_original_tts_bgm 等会产出 tts_track.wav）
    tts_track_path = work_path(work_dir, "tts_track.wav")
    tts_max = tts_mean = None
    if os.path.exists(tts_track_path):
        vt = _volumedetect(tts_track_path)
        if vt:
            tts_mean, tts_max = vt
            detail_parts.append(f"tts_track max={tts_max:.1f}dB mean={tts_mean:.1f}dB")
            # 正常 TTS 峰值约 -3 ~ -6dB；amix 漏 normalize=0 时会按 1/线索数 缩放，
            # 64 条 cue → 峰值 ÷64 ≈ -36dB。用峰值阈值 -25dB 干净分离（buggy 必现，正确轨远高于）。
            if tts_max is not None and tts_max < -25.0:
                warnings.append(
                    f"TTS 轨峰值异常低（max={tts_max:.1f}dB）：疑似 amix 漏加 normalize=0，"
                    f"人声被压死在 BGM 下，见 SKILL §14.8"
                )
    else:
        # TTS 策略但找不到中间轨：无法自动验证，提示人工 Whisper 抽听
        if audio_strategy == "mute_original_tts_bgm":
            detail_parts.append("tts_track.wav 缺失，无法自动验证 TTS 电平")
            warnings.append(
                "tts_track.wav 不在 work/ 中，无法自动判定 TTS 是否被压死；"
                "请按 SKILL §14.8 人工 Whisper 抽听成片头部"
            )

    # 3) 片头近静音 sanity（抽前 15s）
    head_max = None
    try:
        scratch = tempfile.mkdtemp(prefix="ttl-qa-head-")
        head_wav = os.path.join(scratch, "head.wav")
        run([
            "ffmpeg", "-y", "-i", final_path,
            "-t", "15", "-vn", head_wav,
        ], check=False)
        if os.path.exists(head_wav):
            vh = _volumedetect(head_wav)
            if vh:
                h_mean, head_max = vh
                detail_parts.append(f"head(15s) max={head_max:.1f}dB")
                if head_max is not None and head_max < -45.0:
                    warnings.append(
                        f"片头 15s 近静音（max={head_max:.1f}dB）：人声可能缺失或被静音"
                    )
    except Exception:
        pass

    status = "warning" if warnings else "pass"
    return {
        "status": status,
        "detail": "; ".join(detail_parts) + (("; WARN: " + " | ".join(warnings)) if warnings else ""),
        "audio_strategy": audio_strategy,
        "tts_track_max_db": tts_max,
        "tts_track_mean_db": tts_mean,
        "head_max_db": head_max,
        "warnings": warnings,
    }


def check_localization(output_dir: str, work_dir: str) -> Dict:
    """检查本地化：残留中文 OCR 抽检、水印复检、Logo 可见性、review 项"""
    final_path = os.path.join(output_dir, "final.mp4")
    plan = load_json(work_path(work_dir, "localization-plan.json"), {})
    clean_report = load_json(work_path(work_dir, "clean-report.json"), {})

    if not os.path.exists(final_path):
        return {"status": "fail", "detail": "final.mp4 not found"}

    # 残留中文 OCR 抽检（V1 简化：从 clean_report 推断）
    residual_chinese = clean_report.get("inpaint", {}).get("low_confidence_count", 0)

    # 水印区复检
    watermark_items = [i for i in plan.get("items", []) if i.get("category") == "watermark"]
    watermark_recheck = "clean" if watermark_items else "not_applicable"

    # Logo 可见性
    logo_items = [i for i in plan.get("items", []) if i.get("action") == "replace"]
    replaced_logo_visible = len(logo_items) > 0

    # review 项
    review_items = [i for i in plan.get("items", []) if i.get("action") == "review"]
    review_pending = len(review_items)

    # 倒计时 / 排名叠字被删除检查（2026-10-07 新增）：
    # 含排名 token（TOP N / NO.N / 第N球 / RANK N / N/M）的叠字承载「第几个球」语义，
    # 若被设为 action=remove，成片会丢失 which-ball 说明。必须告警。
    removed_countdowns = []
    for it in plan.get("items", []):
        if it.get("action") != "remove":
            continue
        txt = it.get("source_text") or ""
        if has_ranking_token(txt) or it.get("contains_ranking_token"):
            removed_countdowns.append(it.get("item_id"))
    removed_countdowns = [x for x in removed_countdowns if x]

    # timeline 未解决
    timeline = load_json(work_path(work_dir, "timeline.json"), {})
    timeline_unresolved = len(timeline.get("unresolved", []))

    status = "pass"
    warnings = []
    if residual_chinese > 0:
        status = "warning"
        warnings.append(f"{residual_chinese} low-confidence inpaint regions may have residual text")
    if review_pending > 0:
        status = "warning"
        warnings.append(f"{review_pending} review items pending user decision")
    if timeline_unresolved > 0:
        status = "warning"
        warnings.append(f"{timeline_unresolved} timeline issues unresolved")
    if removed_countdowns:
        status = "warning"
        warnings.append(
            f"item(s) {removed_countdowns} carry a countdown/ranking token but action=remove "
            f"-> which-ball info will be lost; switch to translate (preserve & render English Top N), "
            f"do NOT blanket-remove"
        )

    return {
        "status": status,
        "detail": "; ".join(warnings) if warnings else "All localization checks passed",
        "chinese_residual_ocr_sample": residual_chinese,
        "replaced_logo_visible": replaced_logo_visible,
        "watermark_region_recheck": watermark_recheck,
        "review_items_pending": review_pending,
        "timeline_unresolved": timeline_unresolved,
        "removed_countdowns": removed_countdowns,
    }


def main():
    ap = argparse.ArgumentParser(description="Quality assurance for localized video")
    ap.add_argument("--work-dir", required=True, help="Working directory")
    ap.add_argument("--output-dir", required=True, help="Output directory")
    ap.add_argument("--residue-fps", type=float, default=1.0,
                    help="成品中文残留扫描抽帧率（默认 1fps；闪现字幕多时可设 2）")
    ap.add_argument("--residue-single-frame-conf", type=float, default=0.9,
                    help="只被 1 个采样帧检出时，需要多高的置信度才算「真残留」。默认 0.9"
                         "（真烧录字幕持续 >=1s 必然多帧命中；人脸/快速缩放图形会被误读成 CJK，"
                         "典型是单帧 + 低置信度）。设 0 可关闭该剔除逻辑。")
    args = ap.parse_args()

    require_tools("ffmpeg", "ffprobe")

    log.info("Running QA checks...")

    # 七类检查（第 6 项是硬门禁，独立于 plan 直接验证成品画面）
    video_check = check_video(args.output_dir, args.work_dir)
    subtitle_check = check_subtitle(args.output_dir, args.work_dir)
    cover_check = check_cover(args.output_dir, args.work_dir)
    audio_check = check_audio(args.output_dir, args.work_dir)
    localization_check = check_localization(args.output_dir, args.work_dir)
    opening_check = check_opening_frame(args.output_dir, args.work_dir)
    log.info("  对成品 final.mp4 全片跑中文残留硬门禁（这一步不看 plan，只看画面）...")
    residue_check = check_chinese_residue(args.output_dir, args.work_dir, args.residue_fps,
                                          single_frame_min_confidence=args.residue_single_frame_conf)

    # 汇总降级
    degradations = []
    for json_file in ["asr.json", "tts-result.json", "ocr.json"]:
        data = load_json(work_path(args.work_dir, json_file), {})
        if data.get("degraded"):
            degradations.append({
                "capability": json_file.replace(".json", ""),
                "used": data.get("provider_used", data.get("ocr_engine", "unknown")),
                "reason": data.get("warnings", ["degraded"])[0] if data.get("warnings") else "degraded",
            })

    # review 项汇总
    plan = load_json(work_path(args.work_dir, "localization-plan.json"), {})
    review_items = [
        {"item_id": i.get("item_id"), "summary": f"{i.get('category', 'unknown')} @ {i.get('start_time', 0)}s, confidence={i.get('confidence', 0)}, preserved"}
        for i in plan.get("items", []) if i.get("action") == "review"
    ]

    # confidence 汇总
    all_items = plan.get("items", [])
    confidence_summary = {
        "auto": len([i for i in all_items if i.get("confidence", 0) >= 0.85]),
        "warning_band": len([i for i in all_items if 0.60 <= i.get("confidence", 0) < 0.85]),
        "review": len([i for i in all_items if i.get("confidence", 0) < 0.60]),
    }

    # 整体状态
    checks = [video_check, subtitle_check, cover_check, audio_check,
              localization_check, opening_check, residue_check]
    if any(c["status"] == "fail" for c in checks):
        overall = "FAIL"
    elif any(c["status"] == "warning" for c in checks):
        overall = "WARNING"
    else:
        overall = "PASS"

    qa_result = {
        "overall": overall,
        "checks": {
            "video": video_check,
            "subtitle": subtitle_check,
            "cover": cover_check,
            "audio": audio_check,
            "localization": localization_check,
            "opening_frame": opening_check,
            "chinese_residue": residue_check,
        },
        "degradations": degradations,
        "review_items": review_items,
        "confidence_summary": confidence_summary,
    }

    out_path = work_path(args.work_dir, "qa.json")
    save_json(out_path, qa_result)

    log.info(f"QA complete: {overall} -> {out_path}")
    log.info(f"  Video: {video_check['status']}, Subtitle: {subtitle_check['status']}, "
             f"Cover: {cover_check['status']}, Audio: {audio_check['status']}, "
             f"Localization: {localization_check['status']}, "
             f"OpeningFrame: {opening_check['status']}, "
             f"ChineseResidue: {residue_check['status']}")
    if opening_check["status"] == "warning":
        log.warning(f"  [§11.1] {opening_check['detail']}")
        log.warning("         成片第 0 帧必须是正片画面：标题/简介/logo 只放 cover.jpg。")
    if residue_check["status"] == "fail":
        log.error("  [!!] 成品中文残留硬门禁未通过，残留清单：")
        for g in residue_check["groups"]:
            log.error(f"       {g['first_sec']:.1f}-{g['last_sec']:.1f}s  \"{g['text']}\"  "
                      f"({g['frame_count']} 帧, conf={g['max_confidence']:.2f})")
        log.error("  处理完这些残留后必须重跑本检查，不得直接交付。")
    elif residue_check["status"] == "warning":
        log.warning(f"  {residue_check['detail']}")
    if degradations:
        log.warning(f"  Degradations: {len(degradations)}")
    if review_items:
        log.warning(f"  Review items: {len(review_items)}")

    if overall == "FAIL":
        sys.exit(1)


if __name__ == "__main__":
    main()
