#!/usr/bin/env python3
"""
double_check.py - 交付前独立复核（不看 plan，直接对成品下结论）

三项检查（用户 2026-11-03 要求，作为 qa_video.py 之外的第二次确认）：

  1. cover_sanity   封面有没有「花」：糊/涂抹拖影/双层叠字/豆腐块(□)/文字出框/
                    偏色过饱和/整片空白。qa_video 的 cover 检查只看「中文残留 +
                    人脸遮挡 + 安全带」，看不出排版崩坏，这里补上。
  2. yellow_arrow   成品画面里有没有**黄色三角箭头**（源片自带的指示箭头/播放箭头，
                    属于"文字之外的图形残留"）。qa 的 chinese_residue 只查中文，
                    图形类残留一直是盲区。
  3. chinese_residue 中文残留**复扫**，采样率 2fps（qa 用 1fps），且采样相位错开 0.5s，
                    用来发现 qa 网格漏掉的短闪现字幕。

设计原则与 qa_video.py 一致：
  * 引擎不可用不得判通过（不静默降级）。RapidOCR 缺失时 chinese_residue 记
    `degraded: true` 并列进 issues，而不是当作"干净"。
  * 只报事实 + 证据截图路径，不下"能不能交付"的结论 —— 最终裁决留给 Agent。
  * 所有临时帧放在 mkdtemp 目录，不在 work/ 里留垃圾（避免批量删除触发沙箱确认）。

用法：
  python scripts/double_check.py --output-dir output [--work-dir work]
  python scripts/double_check.py --video output/final.mp4 --cover output/cover.jpg
  python scripts/double_check.py --output-dir output --json dc.json --shots-dir /tmp/dc

退出码：0 = 全部干净；1 = 有 fail 级问题；2 = 有 degraded（没查成）
"""

import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import load_json, save_json, log, work_path  # noqa: E402

# ---------------------------------------------------------------- 常量

# 黄：OpenCV HSV H 量程 0-180，纯黄 = 30
YELLOW_HSV_LO = (18, 130, 110)
YELLOW_HSV_HI = (45, 255, 255)

# 三角形状判据
TRI_MIN_AREA = 220          # 720x1280 下 220px² 约等于 15x15 的三角
TRI_MAX_AREA_RATIO = 0.12   # 超过画面 12% 的大黄块不算"箭头"（多半是球衣/地板）
TRI_SOLIDITY_MIN = 0.80     # area / convexHull
TRI_RECT_FILL_LO = 0.26     # 三角形对其外接矩形约占 0.5；矩形/圆会更高
# 2026-11-03：上限从 0.66 提到 0.72。**圆角**三角形（UI 播放键▶）的
# minAreaRect 填充率实测 0.68，被旧上限卡掉；正方形是 1.0，仍然拦得住。
TRI_RECT_FILL_HI = 0.72
# 2026-11-03：饱和阈值从 170 降到 140。实测「乔丹这两个球哪个更难」下方的
# 黄色▶（源片"看完整视频"按钮）sat 均值只有 150，被旧阈值整支漏掉；
# 真正的球衣/地板黄块靠 hue_std 和"持续 ≥2 帧"两条挡着，不靠这一条。
TRI_SAT_MIN = 140
# 判定"确认箭头"所需的最少抽样帧数（2fps 下 6 帧 ≈ 持续 3s）。
# 实测真·播放键 UI 会持续 12s+（24-25 帧）；球衣/地板凑出的三角形很难连续
# 3 秒保持同位置同形状。2026-11-03 从 2 帧提到 6 帧后，误报从 12/24 支降到 2/24 支。
TRI_CONFIRM_FRAMES = 6

TOFU_CHARS = set("□▯▢◻◼▪▫�\ufffd")

CJK_MIN = 2                 # 与 qa_video.has_chinese_text 同口径：至少 2 个汉字


def _has_chinese(text: str, min_chars: int = CJK_MIN) -> bool:
    return sum(1 for c in text if "\u4e00" <= c <= "\u9fff") >= min_chars


def _sha1(path: str) -> str:
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_cover_visual_review(cover_path: str, work_dir: Optional[str] = None,
                              review_path: Optional[str] = None) -> Dict[str, Any]:
    """封面**目视复核**门禁（2026-11-03）。

    为什么必须有这一层（实测证据，别再试图用 OCR 绕过去）：
      「单挑称王」封面中央一枚红色途虎徽标，白色「途」字只有 1 个汉字，
      PP-OCRv4 的 det 阶段**根本不产出这个框** —— 原图 / 2x / 3x / 裁剪 4x /
      det_db_box_thresh 降到 0.1 / det_limit_side_len 1920，全部 0 命中；
      只有局部 Otsu 二值化后能框出框，但识别结果是 'Lh' / '福' 这类垃圾。
      ⇒ 「把 min_chars 从 2 改成 1」并不能补上这个洞：门禁连框都没有。
      图形化汉字（logo / 徽标 / 艺术字）本质是 logo 检测问题，不是 OCR 问题。

    所以这一层的判据不来自算法，而来自**看**：Agent 必须用视觉逐张确认封面，
    并把结论写进 work/cover-review.json。没写 = 没查 = 不能算 pass。

    review 文件格式：
      {"reviewer": "agent", "reviewed_at": "2026-11-03T10:20:00",
       "cover_sha1": "<cover.jpg 的 sha1>", "cjk_seen": false,
       "notes": "全员确认无中文；中央红徽标为源画面 logo，已重出封面"}
    """
    path = review_path or (os.path.join(work_dir, "cover-review.json") if work_dir else None)
    res: Dict[str, Any] = {"status": "warning", "state": "missing", "issues": [],
                           "requires_visual_review": True,
                           "review_file": path}
    if not path:
        res["issues"].append("未提供 work 目录，封面目视复核记录无法定位")
        return res
    if not os.path.exists(path):
        res["issues"].append(
            "封面未经目视复核：OCR 对图形化汉字（logo/徽标里的单字）无效，"
            f"必须看图后写 {os.path.basename(path)}（cjk_seen 字段）")
        return res
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        res.update(state="malformed")
        res["issues"].append(f"cover-review.json 解析失败：{str(e)[:60]}")
        return res

    if not os.path.exists(cover_path):
        res.update(state="malformed")
        return res
    got = str(data.get("cover_sha1") or "")
    want = _sha1(cover_path)
    if got and got != want:
        res.update(state="stale")
        res["issues"].append("封面已重新生成，目视复核记录对应的是旧图（sha1 不符）—— 需要重看")
        return res

    seen = data.get("cjk_seen")
    if seen is None:
        res.update(state="malformed")
        res["issues"].append("cover-review.json 缺少布尔字段 cjk_seen")
        return res
    if bool(seen):
        res.update(status="fail", state="cjk_seen", requires_visual_review=False)
        res["issues"].append(
            "目视复核确认封面有中文/中文徽标："
            + str(data.get("notes") or data.get("suspects") or "")[:160]
            + " —— 必须重出封面或擦除该区域")
        return res

    res.update(status="pass", state="clean", requires_visual_review=False)
    if data.get("reviewer"):
        res["metrics"] = {"reviewer": data.get("reviewer"),
                          "reviewed_at": data.get("reviewed_at")}
    return res


def render_cover_review_sheet(cover_path: str, out_path: str, cols: int = 6,
                              rows: int = 8) -> Optional[str]:
    """画一张带编号网格的封面副本，方便目视时口述/记录坐标。"""
    import cv2
    img = cv2.imread(cover_path)
    if img is None:
        return None
    h, w = img.shape[:2]
    for i in range(1, cols):
        x = int(w * i / cols)
        cv2.line(img, (x, 0), (x, h), (0, 255, 0), 1)
    for j in range(1, rows):
        y = int(h * j / rows)
        cv2.line(img, (0, y), (w, y), (0, 255, 0), 1)
    for c in range(cols):
        for r in range(rows):
            cv2.putText(img, f"{chr(65 + c)}{r + 1}",
                        (int(w * (c + 0.5) / cols) - 14, int(h * (r + 0.5) / rows)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    cv2.imwrite(out_path, img)
    return out_path


def write_cover_review(cover_path: str, work_dir: str, clean: bool,
                       notes: str = "", reviewer: str = "agent") -> Optional[str]:
    """把目视结论落盘成 work/cover-review.json（供 check_cover_visual_review 校验）。"""
    if not cover_path or not os.path.exists(cover_path):
        log.error(f"封面不存在，无法记录目视结论：{cover_path}")
        return None
    from datetime import datetime
    data = {
        "reviewer": reviewer,
        "reviewed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "cover_sha1": _sha1(cover_path),
        "cjk_seen": not clean,
        "notes": notes,
    }
    path = os.path.join(work_dir, "cover-review.json")
    os.makedirs(work_dir, exist_ok=True)
    save_json(path, data)
    log.info(f"  封面目视结论已记录：{path}（cjk_seen={data['cjk_seen']}）")
    return path


def _video_fingerprint(path: str) -> Dict[str, Any]:
    st = os.stat(path)
    return {"size": st.st_size, "mtime": int(st.st_mtime)}


def _crop_letterbox(img, dark_thr: int = 18, row_ratio: float = 0.985,
                    min_keep: float = 0.30):
    """裁掉成片上下（或左右）的大块黑边，让目视时有效像素更大。

    成片是 9:16 竖版画布，源片多是 16:9 横版，中间内容带只占约 55% 高度。
    不裁边直接缩到 360px 宽时，赞助牌上的汉字会糊成一个色块（实测踩过）。
    """
    if img is None or img.size == 0:
        return img
    g = img.mean(axis=2)
    dark = (g < dark_thr).mean(axis=1) >= row_ratio      # 逐行判「几乎全黑」
    rows = np.where(~dark)[0]
    if len(rows):
        y0, y1 = int(rows[0]), int(rows[-1])
        if (y1 - y0 + 1) >= img.shape[0] * min_keep:
            img = img[y0:y1 + 1]
    # 左右黑边（竖屏源放进横版画布的少见情形）同理处理
    g = img.mean(axis=2)
    darkc = (g < dark_thr).mean(axis=0) >= row_ratio
    cols = np.where(~darkc)[0]
    if len(cols):
        x0, x1 = int(cols[0]), int(cols[-1])
        if (x1 - x0 + 1) >= img.shape[1] * min_keep:
            img = img[:, x0:x1 + 1]
    return img


def _crop_blur_bands(img, keep_ratio: float = 0.30, min_saving: float = 0.15):
    """裁掉上下「放大模糊填充」带，只留清晰正片区。

    有些源片把横版正片放进竖版画布，上下用同一帧放大模糊后铺满。模糊区
    不可能承载可辨认的汉字，留着只会让正片被压小、目视失真。
    """
    import cv2
    if img is None or img.size == 0:
        return img
    h = img.shape[0]
    if h < 240:
        return img
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # 逐行锐度：Laplacian 方差的均值（按行分块求，避免逐像素循环）
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    row_score = (lap * lap).mean(axis=1)
    peak = float(row_score.max())
    if peak <= 0:
        return img
    sharp = row_score >= peak * keep_ratio
    # 取最长连续清晰段（正片区一定是最长的那段，模糊带在两端）
    best = (0, -1)
    start = None
    for i, s in enumerate(sharp):
        if s and start is None:
            start = i
        elif not s and start is not None:
            if i - start > best[1] - best[0] + 1:
                best = (start, i - 1)
            start = None
    if start is not None and len(sharp) - start > best[1] - best[0] + 1:
        best = (start, len(sharp) - 1)
    y0, y1 = best
    if y1 - y0 + 1 < h * 0.25:
        return img
    if (y1 - y0 + 1) > h * (1.0 - min_saving):
        return img          # 省不了多少，按原样（避免误裁正常画面）
    return img[y0:y1 + 1]


def build_frame_review_sheet(video: str, out_path: str, n: int = 6,
                             cover_ts: Optional[float] = None,
                             tile_w: int = 640) -> Optional[str]:
    """抽 n 帧（含封面帧）拼成一张带时间戳的联览图，供 Agent 目视。

    用途：中文门禁是 OCR，看不见「红底白字的赞助徽标」这类图形化汉字
    （实测「单挑称王」成品 74s 画面正中仍有途虎徽标，qa 判 PASS）。
    整片逐帧目视不现实，但**徽标通常是持续出现的**，抽 6 帧就能撞见。

    分辨率：先按原生尺寸抽帧、裁掉黑边，再缩到 tile_w 宽。tile_w 低于 600
    时赞助牌上的汉字会被缩糊，目视就失去意义（这曾经是本门禁的真实盲区）。
    """
    import cv2
    from common import run as _run
    dur = _probe_duration(video)
    if dur <= 0:
        return None
    ts = [round(dur * (i + 0.5) / n, 2) for i in range(n)]
    if cover_ts is not None and 0 < cover_ts < dur:
        ts[0] = round(float(cover_ts), 2)
    tmp = tempfile.mkdtemp(prefix="ttl-sheet-")
    tiles = []
    try:
        for t in ts:
            p = os.path.join(tmp, f"t{t}.jpg")
            # 原生分辨率抽帧（不预缩放），裁边后再统一缩放到 tile_w
            _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", video,
                  "-frames:v", "1", "-q:v", "2", p], check=False)
            img = cv2.imread(p)
            if img is None:
                continue
            img = _crop_letterbox(img)
            img = _crop_blur_bands(img)
            h = max(1, int(round(img.shape[0] * tile_w / img.shape[1])))
            interp = cv2.INTER_AREA if img.shape[1] > tile_w else cv2.INTER_CUBIC
            img = cv2.resize(img, (tile_w, h), interpolation=interp)
            cv2.rectangle(img, (0, 0), (img.shape[1] - 1, img.shape[0] - 1), (0, 255, 0), 1)
            cv2.putText(img, f"{t:.1f}s", (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        (0, 255, 0), 2, cv2.LINE_AA)
            tiles.append(img)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not tiles:
        return None
    hh = max(t.shape[0] for t in tiles)
    ww = max(t.shape[1] for t in tiles)
    cols = 2
    rows = (len(tiles) + cols - 1) // cols
    sheet = np.full((rows * hh, cols * ww, 3), 20, np.uint8)
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        sheet[r * hh:r * hh + t.shape[0], c * ww:c * ww + t.shape[1]] = t
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    cv2.imwrite(out_path, sheet)
    return out_path


def check_frame_visual_review(video: str, work_dir: Optional[str] = None,
                              review_path: Optional[str] = None) -> Dict[str, Any]:
    """视频关键帧目视门禁。判据与封面同源：OCR 不构成「无中文」的证据。

    work/frame-review.json：
      {"reviewer":"agent","reviewed_at":"...","video_size":123,"video_mtime":1767...,
       "cjk_seen":false,"notes":"6 帧联览无中文徽标"}
    """
    path = review_path or (os.path.join(work_dir, "frame-review.json") if work_dir else None)
    res: Dict[str, Any] = {"status": "warning", "state": "missing", "issues": [],
                           "requires_visual_review": True, "review_file": path}
    if not path:
        res["issues"].append("未提供 work 目录，关键帧目视记录无法定位")
        return res
    if not os.path.exists(path):
        res["issues"].append(
            "视频关键帧未经目视：OCR 看不见赞助徽标里的图形化汉字"
            "（实测成品 74s 画面中央仍有途虎徽标而 qa 判 PASS）—— 看图后写 frame-review.json")
        return res
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        res.update(state="malformed")
        res["issues"].append(f"frame-review.json 解析失败：{str(e)[:60]}")
        return res
    fp = _video_fingerprint(video)
    if (data.get("video_size") and data["video_size"] != fp["size"]) or \
       (data.get("video_mtime") and data["video_mtime"] != fp["mtime"]):
        res.update(state="stale")
        res["issues"].append("final.mp4 已重新生成，关键帧目视记录对应旧的成片 —— 需要重看")
        return res
    seen = data.get("cjk_seen")
    if seen is None:
        res.update(state="malformed")
        res["issues"].append("frame-review.json 缺少布尔字段 cjk_seen")
        return res
    if bool(seen):
        res.update(status="fail", state="cjk_seen", requires_visual_review=False)
        res["issues"].append("目视确认成片画面有中文/中文徽标："
                             + str(data.get("notes") or "")[:160] + " —— 需补 remove 区域并重渲")
        return res
    res.update(status="pass", state="clean", requires_visual_review=False,
               metrics={"reviewer": data.get("reviewer"),
                        "reviewed_at": data.get("reviewed_at")})
    return res


def write_frame_review(video: str, work_dir: str, clean: bool,
                       notes: str = "", reviewer: str = "agent") -> Optional[str]:
    from datetime import datetime
    fp = _video_fingerprint(video)
    data = {
        "reviewer": reviewer,
        "reviewed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "video_size": fp["size"],
        "video_mtime": fp["mtime"],
        "cjk_seen": not clean,
        "notes": notes,
    }
    path = os.path.join(work_dir, "frame-review.json")
    os.makedirs(work_dir, exist_ok=True)
    save_json(path, data)
    log.info(f"  关键帧目视结论已记录：{path}（cjk_seen={data['cjk_seen']}）")
    return path


def _iou(a: Dict, b: Dict) -> float:
    ax1, ay1, ax2, ay2 = a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]
    bx1, by1, bx2, by2 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
    ix = max(0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union else 0.0


# ================================================================
# 1. 封面 sanity
# ================================================================

def check_cover_sanity(cover_path: str, cjk_min: int = 1,
                       work_dir: Optional[str] = None,
                       review_path: Optional[str] = None) -> Dict[str, Any]:
    """封面「花不花」：糊 / 涂抹拖影 / 叠字 / 豆腐块 / 出框 / 空白。

    这些指标全部是**软信号 + 硬信号分开**：
      hard: 豆腐块、文字出框、文字互相叠压(说明重复绘制)、大面积空白
      soft: 模糊、过饱和、涂抹带（有时是刻意的虚化背景）
    """
    import cv2

    res: Dict[str, Any] = {"status": "pass", "issues": [], "metrics": {}}
    if not cover_path or not os.path.exists(cover_path):
        res.update(status="fail", issues=["cover.jpg 不存在"])
        return res

    img = cv2.imread(cover_path)
    if img is None:
        res.update(status="fail", issues=["封面无法解码（文件损坏）"])
        return res

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # --- 基础统计
    black_ratio = float((gray < 12).mean())
    white_ratio = float((gray > 243).mean())
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sat_mean = float(hsv[:, :, 1].mean())

    res["metrics"].update({
        "size": [w, h],
        "aspect": round(w / h, 4) if h else 0,
        "black_ratio": round(black_ratio, 4),
        "white_ratio": round(white_ratio, 4),
        "laplacian_var": round(lap_var, 2),
        "saturation_mean": round(sat_mean, 2),
    })

    # --- 硬信号 1：整片几乎空白/纯黑（渲染失败最典型的样子）
    if black_ratio > 0.88:
        res["issues"].append(f"封面 {black_ratio:.0%} 是近黑像素（疑似渲染空白）")
        res["status"] = "fail"
    if white_ratio > 0.90:
        res["issues"].append(f"封面 {white_ratio:.0%} 是近白像素（疑似丢图/白屏）")
        res["status"] = "fail"

    # --- 硬信号 2：涂抹/糊块（局部高频能量塌陷，且亮度处于中段）
    #     故障形态：inpaint 把一块字糊成中灰/中彩的软斑，面积通常 >= 1% 画面。
    #     为什么不用"相邻行相同"或"局部方差低"：
    #       ① 虚化背景（人群/球场 out-of-focus）本来就平滑 → 全量误报（21/21）；
    #       ② 补 9:16 加的 letterbox 黑边也是平坦区 → 同样误报（21/21）。
    #     改用 32x32 块的**高频能量**并与全图中位数比：糊块能量掉到中位数的 15% 以下、
    #     亮度落在 45–210（排除黑边/高光），且邻块能量正常 —— 三个条件同时满足才算。
    lap = np.abs(cv2.Laplacian(gray.astype(np.float32), cv2.CV_32F))
    BS = 32
    gw, gh = max(1, w // BS), max(1, h // BS)
    energy_grid = cv2.resize(cv2.blur(lap, (BS, BS)), (gw, gh), interpolation=cv2.INTER_AREA)
    bright_grid = cv2.resize(cv2.blur(gray.astype(np.float32), (BS, BS)), (gw, gh),
                             interpolation=cv2.INTER_AREA)
    med = float(np.median(energy_grid)) or 1.0
    blur_blocks = ((energy_grid < 0.15 * med)
                   & (bright_grid > 45) & (bright_grid < 210)).astype(np.uint8)
    n_lbl, lbl, stats, _ = cv2.connectedComponentsWithStats(blur_blocks, 8)
    block_area = float(BS * BS)
    smears = []
    for i in range(1, n_lbl):
        blk = int(stats[i, cv2.CC_STAT_AREA])          # 块数
        if blk < 24:                                    # <24 块 ≈ 1.2% 画面，忽略
            continue
        gx0, gy0 = int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP])
        gw0, gh0 = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
        bx, by = gx0 * BS, gy0 * BS
        bxx, byy = min(w, (gx0 + gw0) * BS), min(h, (gy0 + gh0) * BS)
        if bxx <= bx or byy <= by:
            continue
        pad = 2
        ring = energy_grid[max(0, gy0 - pad):min(gh, gy0 + gh0 + pad),
                           max(0, gx0 - pad):min(gw, gx0 + gw0 + pad)]
        inner = energy_grid[gy0:gy0 + gh0, gx0:gx0 + gw0]
        if ring.size == 0 or inner.size == 0:
            continue
        ring_e, inner_e = float(ring.mean()), float(inner.mean())
        if ring_e > 2.0 * max(inner_e, 0.01):
            smears.append({"bbox": [bx, by, bxx - bx, byy - by],
                           "area_ratio": round(blk * block_area / (w * h), 3),
                           "inner_energy": round(inner_e, 3),
                           "ring_energy": round(ring_e, 3)})
    if smears:
        res["metrics"]["smears"] = smears[:5]
        big = max(smears, key=lambda s: s["area_ratio"])
        res["issues"].append(
            f"封面有 {len(smears)} 处疑似涂抹/糊块（最大 {big['area_ratio']:.1%}，位于 {big['bbox']}）"
            "—— 需目视该 bbox 确认（自动判定保守：只报面积 >=1.2% 且周围细节明显更多的块）")
        if res["status"] == "pass":
            res["status"] = "warning"

    # --- 软信号：块状伪影（重度压缩/重复编码）
    v = gray.astype(np.float32)
    gx = np.abs(np.diff(v, axis=1)).mean(axis=0)
    cols8 = np.array([gx[i] for i in range(len(gx)) if i % 8 == 7])
    cols_other = np.array([gx[i] for i in range(len(gx)) if i % 8 != 7])
    if len(cols8) and len(cols_other) and cols_other.mean() > 0:
        blockiness = float(cols8.mean() / cols_other.mean())
        res["metrics"]["blockiness"] = round(blockiness, 3)
        if blockiness > 1.45:
            res["issues"].append(f"封面块状伪影偏强（blockiness={blockiness:.2f}），疑似重度压缩/重复编码")
            if res["status"] == "pass":
                res["status"] = "warning"

    # --- 软信号：糊
    if lap_var < 25:
        res["issues"].append(f"封面清晰度偏低（Laplacian var={lap_var:.1f}），疑似放大或重度模糊")
        if res["status"] == "pass":
            res["status"] = "warning"
    if sat_mean > 165:
        res["issues"].append(f"封面饱和度过高（{sat_mean:.0f}），疑似偏色")
        if res["status"] == "pass":
            res["status"] = "warning"

    # --- OCR：豆腐块 / 出框 / 叠字
    ocr_ok = True
    try:
        from ocr_video import ocr_with_rapidocr
    except Exception as e:  # pragma: no cover
        ocr_ok = False
        res["issues"].append(f"OCR 不可用（{str(e)[:60]}），封面文字类检测被跳过")
        res["metrics"]["ocr"] = "degraded"

    if ocr_ok:
        try:
            dets = ocr_with_rapidocr(cover_path)
        except Exception as e:
            dets = []
            res["issues"].append(f"封面 OCR 失败：{str(e)[:80]}")
            res["metrics"]["ocr"] = "degraded"
        boxes = []
        for d in dets:
            t = (d.get("text") or "").strip()
            if not t:
                continue
            boxes.append({"text": t, "bbox": d["bbox"], "conf": d.get("confidence") or 0})
        res["metrics"]["text_lines"] = [{"text": b["text"][:40], "bbox": b["bbox"]} for b in boxes]

        # 中文残留（封面应全英文）。cjk_min=1 时连单个汉字也报：
        # 2026-11-03 实测「单挑称王」封面中央带一枚红色途虎 logo，logo 里的「途」
        # 只有 1 个字，qa_video 的 has_chinese_text(min_chars=2) 直接放过了。
        cjk = [b["text"] for b in boxes if _has_chinese(b["text"], cjk_min)]
        if cjk:
            res["issues"].append(f"封面残留中文：{cjk[:4]}")
            res["status"] = "fail"

        # 豆腐块 / 乱码替换符
        tofu = [b["text"] for b in boxes if any(c in TOFU_CHARS for c in b["text"])]
        if tofu:
            res["issues"].append(f"封面出现豆腐块/替换符字形：{tofu[:4]}（字体缺字）")
            res["status"] = "fail"

        # 出框（留 1% 容差）
        out = [b for b in boxes
               if b["bbox"]["x"] < -w * 0.01 or b["bbox"]["y"] < -h * 0.01
               or b["bbox"]["x"] + b["bbox"]["w"] > w * 1.01
               or b["bbox"]["y"] + b["bbox"]["h"] > h * 1.01]
        if out:
            res["issues"].append(f"{len(out)} 行文字超出画面边界：{[o['text'][:16] for o in out][:3]}")
            res["status"] = "fail"

        # 叠字：两个框 IoU>0.15 且文本不同（同一处被画了两遍/两套文案重叠）
        overlaps = []
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if boxes[i]["text"] == boxes[j]["text"]:
                    continue
                if _iou(boxes[i]["bbox"], boxes[j]["bbox"]) > 0.15:
                    overlaps.append((boxes[i]["text"][:16], boxes[j]["text"][:16]))
        if overlaps:
            res["issues"].append(f"封面疑似叠字（两套文字重叠）：{overlaps[:3]}")
            res["status"] = "fail"

    # --- 比例（短视频封面必须 9:16）
    if h and abs(w / h - 9 / 16) > 0.02:
        res["issues"].append(f"封面比例不是 9:16（{w}x{h}）")
        if res["status"] == "pass":
            res["status"] = "warning"

    # --- 目视复核门禁：OCR 查不到的图形化汉字，只能靠看
    review = check_cover_visual_review(cover_path, work_dir, review_path)
    res["visual_review"] = review
    res["requires_visual_review"] = review.get("requires_visual_review", True)
    if review.get("issues"):
        res["issues"].extend(review["issues"])
    if review["status"] == "fail":
        res["status"] = "fail"
    elif review["status"] == "warning" and res["status"] == "pass":
        res["status"] = "warning"

    return res


# ================================================================
# 2. 黄色三角箭头
# ================================================================

def _find_yellow_triangles(bgr: np.ndarray) -> List[Dict[str, Any]]:
    """在一帧里找"像箭头/播放键"的实心黄色三角形。"""
    import cv2

    h, w = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(YELLOW_HSV_LO), np.array(YELLOW_HSV_HI))
    # 闭运算补掉描边镂空，避免三角形被拆成碎块
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    hits = []
    for c in cnts:
        area = cv2.contourArea(c)
        if area < TRI_MIN_AREA or area > w * h * TRI_MAX_AREA_RATIO:
            continue
        peri = cv2.arcLength(c, True)
        # 2026-11-03：原来只认「approx 恰好 3 点」。圆角三角形（播放键▶ 带圆角）
        # 会得到 4 点，被整支漏掉。改为退火式尝试多个 eps，取点数最少的 3–4 点解；
        # 4 点时把最短边的两端合并成一个顶点再算内角。
        approx = None
        for eps in (0.02, 0.03, 0.04, 0.05, 0.07):
            ap = cv2.approxPolyDP(c, eps * peri, True)
            if len(ap) == 3:
                approx = ap
                break
            if len(ap) == 4 and approx is None:
                approx = ap
            # 2 点/1 点（细长条纹被退火成一条线）直接丢弃，不能当三角形
        if approx is None or len(approx) not in (3, 4):
            continue
        if len(approx) == 4:
            pts4 = approx.reshape(-1, 2).astype(np.float64)
            best = None
            for i in range(4):
                a, b = pts4[i], pts4[(i + 1) % 4]
                d = float(np.linalg.norm(a - b))
                if best is None or d < best[0]:
                    best = (d, i)
            i = best[1]
            merged = (pts4[i] + pts4[(i + 1) % 4]) / 2
            keep = [pts4[j] for j in range(4) if j not in (i, (i + 1) % 4)]
            approx = np.array([merged] + keep, dtype=np.int32).reshape(-1, 1, 2)
        hull = cv2.convexHull(c)
        hull_area = cv2.contourArea(hull)
        if hull_area <= 0 or area / hull_area < TRI_SOLIDITY_MIN:
            continue
        (cx, cy), (rw, rh), _ = cv2.minAreaRect(c)
        rect_area = rw * rh
        if rect_area <= 0:
            continue
        fill = area / rect_area
        if not (TRI_RECT_FILL_LO <= fill <= TRI_RECT_FILL_HI):
            continue
        # 极端长条（细线）排除
        if min(rw, rh) > 0 and max(rw, rh) / max(1.0, min(rw, rh)) > 4.5:
            continue
        # 尺寸上限：真正的 UI 箭头不会占画面 1/8 以上
        if rw > w * 0.12 or rh > h * 0.12:
            continue
        # 三个内角都要像三角形（30°–100°）：球衣色块凑出的三角形经常有个 120°+ 的钝角
        angs = []
        pts = approx.reshape(-1, 2).astype(np.float64)
        for i in range(3):
            p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % 3]
            v1, v2 = p0 - p1, p2 - p1
            n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
            if n1 < 1e-6 or n2 < 1e-6:
                angs = []
                break
            angs.append(float(np.degrees(np.arccos(np.clip(v1 @ v2 / (n1 * n2), -1, 1)))))
        if not angs or min(angs) < 20 or max(angs) > 110 or sum(angs) < 150:
            continue
        # 颜色必须“纯”：叠加的箭头是单色实心；球场 logo / 球衣是渐变杂色
        x, y, bw, bh = cv2.boundingRect(c)
        sub = hsv[y:y + bh, x:x + bw]
        sub_m = mask[y:y + bh, x:x + bw] > 0
        if sub_m.sum() < 50:
            continue
        hue_std = float(sub[:, :, 0][sub_m].std())
        sat_mean = float(sub[:, :, 1][sub_m].mean())
        if hue_std > 8 or sat_mean < TRI_SAT_MIN:
            continue
        # 2026-11-03：加了「周围是中性色」这条，才敢放宽形状阈值。
        #   UI 箭头（播放键▶/指示箭头）永远贴在**深色或灰白的条/底**上（实测
        #   「乔丹这两个球」的 ▶ 就压在一根深灰条上，ring 中性色占比 0.8+）；
        #   球衣、地板、logo 里的黄三角周围一定是彩色，中性占比很低。
        #   放宽 fill/sat 后单靠「持续 ≥2 帧」压不住误报（实测 17/24 支误报，
        #   人类晃倒 338 个候选），这条才是真正区分"叠加 UI"和"画面内容"的判据。
        pad = max(4, int(round(max(bw, bh) * 0.35)))
        y0, y1 = max(0, y - pad), min(h, y + bh + pad)
        x0, x1 = max(0, x - pad), min(w, x + bw + pad)
        ring = hsv[y0:y1, x0:x1]
        ring_mask = np.ones(ring.shape[:2], bool)
        ring_mask[y - y0:y - y0 + bh, x - x0:x - x0 + bw] = False
        if ring_mask.sum() < 40:
            continue
        rs, rv = ring[:, :, 1][ring_mask], ring[:, :, 2][ring_mask]
        neutral = float(((rs < 70) | (rv < 55)).mean())
        if neutral < 0.45:
            continue
        hits.append({
            "bbox": {"x": int(x), "y": int(y), "w": int(bw), "h": int(bh)},
            "area": int(area),
            "fill": round(float(fill), 3),
            "hue_std": round(hue_std, 2),
            "ring_neutral": round(neutral, 3),
            "rect_fill": [round(rw, 1), round(rh, 1)],
        })
    return hits


_OCR_PAGE_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def _drop_text_glyphs(windows: List[Dict[str, Any]]):
    """把落在 OCR 文字行内的三角窗口判为「字母 A」剔掉。

    每支片只对窗口首帧跑一次 OCR（结果按帧路径缓存），成本可忽略。
    """
    out: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for w in windows:
        b = w["samples"][0]["bbox"]
        cx = b["x"] + b["w"] / 2.0
        cy = b["y"] + b["h"] / 2.0
        fp = w["samples"][0]["frame"]
        dets = _OCR_PAGE_CACHE.get(fp)
        if dets is None:
            try:
                from ocr_video import ocr_with_rapidocr
                dets = ocr_with_rapidocr(fp)
            except Exception:
                dets = []
            _OCR_PAGE_CACHE[fp] = dets
        in_line = False
        for d in dets:
            t = (d.get("text") or "").strip()
            tb = d.get("bbox") or {}
            if len(t) < 2 or (d.get("confidence") or 0) < 0.6:
                continue
            x, y = tb.get("x", 0), tb.get("y", 0)
            if (x - 2 <= cx <= x + tb.get("w", 0) + 2
                    and y - 2 <= cy <= y + tb.get("h", 0) + 2):
                in_line = True
                w["text_line"] = t
                break
        (dropped if in_line else out).append(w)
    return out, dropped


def check_yellow_arrows(video_path: str, frame_dir: str, fps: float,
                        shots_dir: Optional[str] = None) -> Dict[str, Any]:
    """扫成品全片，找黄色三角箭头。

    ⭐ 分两档（2026-11-03 首版全是误报后定的口径）：
      真实叠加图形（播放键 / 指示箭头 / 贴纸）在时间上是**稳定**的，
      2fps 采样必然连续命中 ≥2 帧；而黄色球衣、地板反光、logo 色块这类
      画面内元素会随镜头运动变形，通常只单帧偶然凑成三角形。
      实测「无奖竞猜」64s：4 处命中全部单帧、bbox 12x40~118x70、位置互不相邻，
      抽帧目视确认画面里没有任何箭头（是湖人黄色球衣与地板）。
      ⇒ confirmed（≥2 帧且同位置）才算 warning；single（单帧）只进 candidates，
        不入 issues —— 否则每支片都要人类看 4 张截图，双检就废了。
    """
    import cv2

    res: Dict[str, Any] = {"status": "pass", "fps": fps, "windows": [], "candidates": [],
                           "hits": 0, "frames_scanned": 0, "issues": []}
    frames = sorted(glob.glob(os.path.join(frame_dir, "f*.jpg")))
    if not frames:
        res["status"] = "degraded"
        res["issues"].append("未抽到帧，黄色箭头检测被跳过")
        return res

    per_frame = []
    for i, fp in enumerate(frames):
        img = cv2.imread(fp)
        if img is None:
            continue
        res["frames_scanned"] += 1
        ts = round(i / fps, 3)
        hits = _find_yellow_triangles(img)
        if hits:
            per_frame.append((ts, fp, hits))

    res["hits"] = sum(len(h) for _, _, h in per_frame)
    if not per_frame:
        return res

    # 归并：时间间隔 <=1.5s 且 bbox 与已有窗内某帧 IoU >=0.25 视为同一处图形
    windows: List[Dict[str, Any]] = []
    for ts, fp, hits in per_frame:
        for h in hits:
            matched = None
            for w in windows:
                if ts - w["last"] > 1.5:
                    continue
                if any(_iou(h["bbox"], prev["bbox"]) >= 0.25 for prev in w["samples"]):
                    matched = w
                    break
            if matched is None:
                windows.append({"first": ts, "last": ts, "frames": 1,
                                "samples": [{"t": ts, "frame": fp, "bbox": h["bbox"],
                                             "fill": h["fill"]}]})
            else:
                matched["last"] = ts
                matched["frames"] += 1
                matched["samples"].append({"t": ts, "frame": fp, "bbox": h["bbox"],
                                           "fill": h["fill"]})

    confirmed = [w for w in windows if w["frames"] >= TRI_CONFIRM_FRAMES]
    single = [w for w in windows if w["frames"] < TRI_CONFIRM_FRAMES]

    if shots_dir:
        os.makedirs(shots_dir, exist_ok=True)
        for wi, wd in enumerate(confirmed + single):
            tag = "arrow" if wd["frames"] >= 2 else "suspect"
            src = wd["samples"][0]["frame"]
            try:
                shutil.copyfile(
                    src, os.path.join(shots_dir, f"{tag}_w{wi:02d}_t{wd['first']:.1f}s.jpg"))
            except Exception:
                pass

    def _pack(w):
        return {"first_sec": w["first"], "last_sec": w["last"], "frames": w["frames"],
                "bbox": w["samples"][0]["bbox"], "fill": w["samples"][0]["fill"]}

    # ⭐ 第三层过滤：排除「我们自己的黄字」。
    # 品牌字 "HOOP DYNASTY DAILY" 的 A、字幕 "…AN POWER" 的 A，字形本身就是
    # 个完美三角形 —— 实测「纯享」640 帧、「运动天赋」42 帧稳定命中，全是误报。
    # 判据：bbox 中心落在**同帧 OCR 认出的文字行**框内 → 判定为字母，剔除。
    # （限制：球场记分牌 logo 里的黄球仍会留到 confirmed，必须目视，见 SKILL.md）
    confirmed, glyphs = _drop_text_glyphs(confirmed)
    single, _ = _drop_text_glyphs(single)

    res["windows"] = [_pack(w) for w in confirmed]
    res["candidates"] = [_pack(w) for w in single]
    if glyphs:
        res["text_glyphs_dropped"] = [
            {"first_sec": w["first"], "text": w.get("text_line", ""),
             "bbox": w["samples"][0]["bbox"]} for w in glyphs]

    if confirmed:
        res["status"] = "warning"
        res["issues"].append(
            "检出 %d 处**稳定存在**的黄色三角箭头（%s）—— 疑似源片图形残留，必须抽帧目视"
            % (len(confirmed), ", ".join(f"{w['first']:.1f}-{w['last']:.1f}s" for w in confirmed[:6])))
    return res


# ================================================================
# 3. 中文残留复扫
# ================================================================

def check_chinese_residue(final_path: str, frame_dir: str, fps: float,
                          ocr_every: int = 1, min_conf: float = 0.5,
                          cjk_min: int = CJK_MIN) -> Dict[str, Any]:
    """独立复扫中文，采样相位与 qa_video（1fps、整数秒）错开。"""
    res: Dict[str, Any] = {"status": "pass", "fps": fps, "ocr_every": ocr_every,
                           "frames_ocr": 0, "groups": [], "issues": []}
    try:
        from ocr_video import ocr_with_rapidocr
    except Exception as e:
        res.update(status="degraded")
        res["issues"].append(f"RapidOCR 不可用（{str(e)[:60]}）—— 本次不构成「无中文」的证据")
        return res

    frames = sorted(glob.glob(os.path.join(frame_dir, "f*.jpg")))
    if not frames:
        res["status"] = "degraded"
        res["issues"].append("未抽到帧")
        return res

    groups: Dict[str, Dict[str, Any]] = {}
    every_n = max(1, ocr_every)
    step = max(1, every_n)
    for i, fp in enumerate(frames):
        if every_n > 1 and i % every_n:
            continue
        ts = round(i / fps, 3)
        if res["frames_ocr"] and res["frames_ocr"] % 25 == 0:
            log.info(f"    OCR 进度 {res['frames_ocr']}/{len(frames)//step} @ {ts:.1f}s")
        try:
            dets = ocr_with_rapidocr(fp)
        except Exception:
            continue
        res["frames_ocr"] += 1
        for d in dets:
            t = (d.get("text") or "").strip()
            conf = d.get("confidence") or 0
            if not t or conf < min_conf or not _has_chinese(t, 1):
                continue
            key = t.replace(" ", "")
            g = groups.get(key)
            if g is None:
                groups[key] = {"text": t, "first_sec": ts, "last_sec": ts, "frames": 1,
                               "max_confidence": conf, "bbox": d["bbox"]}
            else:
                g["last_sec"] = ts
                g["frames"] += 1
                g["max_confidence"] = max(g["max_confidence"], conf)

    out = sorted(groups.values(), key=lambda g: -g["frames"])
    # 与 qa_video.check_chinese_residue 同口径的窄规则：真烧录字幕至少持续 ~1s，
    # 2fps 采样必然命中 ≥2 帧；只出现 1 帧且置信度 <0.9 的多半是人脸/球衣纹理误读。
    def _nchars(g):
        return sum(1 for c in g["text"] if "\u4e00" <= c <= "\u9fff")

    # 单字中文单独成一档（2026-11-03）：qa_video 的 has_chinese_text 要求 ≥2 汉字，
    # 于是「途虎」这类 logo 里的单个「途」被系统性放过（单挑称王封面正中那枚红 logo）。
    # 单字噪声也大（球衣纹理常被读成「热」「球」），所以只在高置信且持续 ≥3 帧时
    # 报 warning，让 Agent 抽帧看那一个 bbox，而不是直接判 FAIL。
    multi = [g for g in out if _nchars(g) >= cjk_min]
    single = [g for g in out if _nchars(g) < cjk_min
              and g["frames"] >= 3 and g["max_confidence"] >= 0.9]
    confirmed = [g for g in multi if g["frames"] >= 2 or g["max_confidence"] >= 0.9]
    suspects = [g for g in out if g not in confirmed and g not in single]
    res["groups"] = confirmed
    res["single_char"] = single
    res["suspects"] = suspects
    if confirmed:
        res["status"] = "fail"
        res["issues"].append(
            f"复扫检出 {len(confirmed)} 组中文残留（≥2 帧或高置信）：" +
            ", ".join(f"{g['text'][:10]}({g['first_sec']}-{g['last_sec']}s)" for g in confirmed[:5]))
    if single:
        res["status"] = "warning" if res["status"] == "pass" else res["status"]
        res["issues"].append(
            f"{len(single)} 处单字中文（高置信且持续 ≥3 帧，疑似 logo/水印里的单字）：" +
            ", ".join(f"「{g['text'][:2]}」{g['first_sec']}-{g['last_sec']}s bbox={g['bbox']}"
                      for g in single[:3]) + " —— 需抽帧目视")
    if suspects:
        res["issues"].append(
            f"{len(suspects)} 组单帧低置信候选（多为纹理误读，需抽帧确认）：" +
            ", ".join(f"{g['text'][:8]}@{g['first_sec']}s(conf{g['max_confidence']:.2f})"
                      for g in suspects[:5]))
    return res


# ================================================================
# 抽帧
# ================================================================

def extract_frames(video_path: str, out_dir: str, fps: float) -> Tuple[int, float]:
    """按 fps 抽帧（含第 0 帧）。返回帧数。

    刻意不用 `-vf fps=N`：fps filter 的输出不含第 0 帧（实测首帧落在 t=0.47，
    整条时间轴比标注晚约 0.53s），qa_video.py 踩过这个坑（假 PASS）。
    这里用 `select='not(mod(n,period))'` + vsync 0，第 0 帧必在。
    """
    from common import run
    os.makedirs(out_dir, exist_ok=True)
    src_fps = _probe_fps(video_path)
    period = max(1, int(round(src_fps / fps))) if fps > 0 else 1
    real_fps = src_fps / period
    run(["ffmpeg", "-y", "-v", "error", "-i", video_path,
         "-vf", f"select='not(mod(n,{period}))'", "-vsync", "0", "-q:v", "3",
         os.path.join(out_dir, "f%05d.jpg")], check=False)
    log.info(f"  抽帧：src {src_fps:g}fps / period {period} => 实际 {real_fps:.2f}fps")
    n = len(glob.glob(os.path.join(out_dir, "f*.jpg")))
    return n, real_fps


def _probe_fps(video: str, fallback: float = 30.0) -> float:
    from common import run
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", video], check=False)
    try:
        num, _, den = (r.stdout or "").strip().partition("/")
        v = float(num) / float(den or 1)
        return v if v > 0 else fallback
    except Exception:
        return fallback


def _probe_duration(video: str, fallback: float = 0.0) -> float:
    from common import run
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video], check=False)
    try:
        v = float((r.stdout or "").strip())
        return v if v > 0 else fallback
    except Exception:
        return fallback


# ================================================================
# 主流程
# ================================================================

def _resolve(output_dir: Optional[str], work_dir: Optional[str],
             video: Optional[str], cover: Optional[str]) -> Tuple[str, str, Optional[str]]:
    if video is None and output_dir:
        video = os.path.join(output_dir, "final.mp4")
    if cover is None:
        if output_dir:
            cover = os.path.join(output_dir, "cover.jpg")
    if work_dir is None and output_dir:
        cand = os.path.join(output_dir, "work")
        work_dir = cand if os.path.isdir(cand) else None
    return video or "", cover or "", work_dir


def run(output_dir: Optional[str] = None, work_dir: Optional[str] = None,
        video: Optional[str] = None, cover: Optional[str] = None,
        fps: float = 2.0, ocr_every: int = 2, json_out: Optional[str] = None,
        shots_dir: Optional[str] = None, cjk_min: int = CJK_MIN,
        cover_mark: bool = False, review_path: Optional[str] = None,
        frame_mark: bool = False, frame_review: Optional[str] = None,
        sheet_w: int = 640) -> Dict[str, Any]:
    video, cover, work_dir = _resolve(output_dir, work_dir, video, cover)

    result: Dict[str, Any] = {
        "video": video, "cover": cover, "fps": fps,
        "cover_sanity": {}, "yellow_arrow": {}, "chinese_residue": {},
        "overall": "pass", "issues": [],
    }
    if not video or not os.path.exists(video):
        result["overall"] = "fail"
        result["issues"].append(f"final.mp4 不存在：{video}")
        if json_out:
            save_json(json_out, result)
        return result

    # 封面默认 cjk_min=1：封面文字是我们自己用英文字体画的，出现任何单个汉字
    # 都只可能来自源画面（logo / 水印刷漏），不存在"纹理误读"的干扰。
    result["cover_sanity"] = check_cover_sanity(cover, 1, work_dir=work_dir,
                                                review_path=review_path)

    # 目视辅助图（带 A1..F8 网格编号），Agent 看图时可直接报格子坐标
    if cover_mark and cover and os.path.exists(cover):
        out = os.path.join(work_dir or os.path.dirname(cover), "cover-check",
                           "cover-grid.jpg")
        marked = render_cover_review_sheet(cover, out)
        if marked:
            result["cover_review_sheet"] = marked
            log.info(f"  封面目视辅助图：{marked}")

    tmp = tempfile.mkdtemp(prefix="ttl-doublecheck-")
    try:
        log.info(f"抽帧中（目标 {fps}fps）→ {tmp}")
        n, real_fps = extract_frames(video, tmp, fps)
        log.info(f"  抽出 {n} 帧")
        result["frames"] = n
        result["fps"] = real_fps
        if n == 0:
            result["overall"] = "fail"
            result["issues"].append("抽帧失败，视频可能损坏")
        else:
            result["yellow_arrow"] = check_yellow_arrows(video, tmp, real_fps, shots_dir)
            result["chinese_residue"] = check_chinese_residue(video, tmp, real_fps, ocr_every,
                                                             cjk_min=cjk_min)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 关键帧目视门禁：OCR 看不见赞助徽标里的图形化汉字（成品 74s 实拍验证过）
    result["frame_review"] = check_frame_visual_review(video, work_dir, frame_review)
    if frame_mark:
        ts = None
        if cover:
            cp = work_path(work_dir, "cover-plan.json") if work_dir else None
            if cp and os.path.exists(cp):
                ts = (load_json(cp, {}).get("selected_frame") or {}).get("timestamp_sec")
        sheet = os.path.join(work_dir or os.path.dirname(video), "cover-check",
                             "frame-sheet.jpg")
        got = build_frame_review_sheet(video, sheet, cover_ts=ts, tile_w=sheet_w)
        if got:
            result["frame_review_sheet"] = got
            log.info(f"  关键帧联览图：{got}")

    statuses = [result["cover_sanity"].get("status"),
                result["yellow_arrow"].get("status"),
                result["chinese_residue"].get("status"),
                result["frame_review"].get("status")]
    result["issues"] = (result["cover_sanity"].get("issues", [])
                        + result["yellow_arrow"].get("issues", [])
                        + result["chinese_residue"].get("issues", [])
                        + result["frame_review"].get("issues", []))
    if "fail" in statuses:
        result["overall"] = "fail"
    elif "degraded" in statuses:
        result["overall"] = "degraded"
    elif "warning" in statuses:
        result["overall"] = "warning"

    if json_out:
        save_json(json_out, result)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="交付前独立复核：封面花屏 / 黄色三角箭头 / 中文残留复扫")
    ap.add_argument("--output-dir", help="成品目录（含 final.mp4 + cover.jpg + work/）")
    ap.add_argument("--work-dir", help="work 目录（可选，仅用于读取 probe）")
    ap.add_argument("--video", help="直接指定 final.mp4")
    ap.add_argument("--cover", help="直接指定 cover.jpg")
    ap.add_argument("--fps", type=float, default=2.0, help="抽帧采样率，默认 2.0")
    ap.add_argument("--ocr-every", type=int, default=2,
                    help="每隔 N 个采样帧做一次 OCR，默认 2（=2fps 抽帧、1fps OCR，且相位比 qa 的整数秒错开 0.5s）")
    ap.add_argument("--json", help="结果写入路径")
    ap.add_argument("--shots-dir", help="黄色箭头命中帧另存目录（便于目视）")
    ap.add_argument("--cjk-min", type=int, default=2,
                    help="中文残留判定所需最少汉字数，默认 2（与 qa_video 同口径）；"
                         "封面固定用 1")
    ap.add_argument("--cover-mark", action="store_true",
                    help="额外输出带 A1..F8 网格的封面副本 work/cover-check/cover-grid.jpg"
                         "（目视复核时用来说坐标）")
    ap.add_argument("--review", help="封面目视复核 JSON 路径，默认 <work>/cover-review.json")
    ap.add_argument("--record-review", choices=["clean", "cjk"],
                    help="把目视结论写入 cover-review.json 后退出（clean=确认无中文，cjk=有中文）")
    ap.add_argument("--notes", default="", help="配合 --record-review 记录的说明")
    ap.add_argument("--frame-mark", action="store_true",
                    help="抽 6 帧（含封面帧）拼成 work/cover-check/frame-sheet.jpg 供目视")
    ap.add_argument("--sheet-w", type=int, default=640,
                    help="联览图每帧宽度，默认 640（低于 600 汉字会被缩糊，目视失去意义）")
    ap.add_argument("--record-frame-review", choices=["clean", "cjk"],
                    help="把关键帧目视结论写入 frame-review.json 后退出")
    ap.add_argument("--frame-review", help="关键帧目视 JSON 路径，默认 <work>/frame-review.json")
    args = ap.parse_args()

    if args.record_review or args.record_frame_review:
        cover = args.cover or (os.path.join(args.output_dir, "cover.jpg")
                               if args.output_dir else None)
        work = args.work_dir or (os.path.join(args.output_dir, "work")
                                 if args.output_dir else None)
        if not work:
            ap.error("--record-* 需要 --output-dir（或同时给 --work-dir）")
        ok = True
        if args.record_review:
            if not cover:
                ap.error("--record-review 需要 cover.jpg（--output-dir 或 --cover）")
            ok = bool(write_cover_review(cover, work, args.record_review == "clean", args.notes)) and ok
        if args.record_frame_review:
            video = args.video or (os.path.join(args.output_dir, "final.mp4")
                                   if args.output_dir else None)
            if not video or not os.path.exists(video):
                ap.error("--record-frame-review 需要 final.mp4")
            ok = bool(write_frame_review(video, work, args.record_frame_review == "clean",
                                         args.notes)) and ok
        return 0 if ok else 1

    r = run(args.output_dir, args.work_dir, args.video, args.cover,
            args.fps, args.ocr_every, args.json, args.shots_dir, args.cjk_min,
            cover_mark=args.cover_mark, review_path=args.review,
            frame_mark=args.frame_mark, frame_review=args.frame_review,
            sheet_w=args.sheet_w)

    print(f"\n=== double check: {os.path.basename(os.path.dirname(r['video'])) or r['video']}")
    cs = r["cover_sanity"]
    print(f"  cover_sanity    {cs.get('status','?'):8s} {cs.get('metrics',{}).get('size','')}")
    for i in cs.get("issues", []):
        print(f"      - {i}")
    ya = r["yellow_arrow"]
    print(f"  yellow_arrow    {ya.get('status','?'):8s} frames={ya.get('frames_scanned',0)} hits={ya.get('hits',0)}")
    for w in ya.get("windows", [])[:8]:
        print(f"      ! 稳定箭头 {w['first_sec']:.1f}-{w['last_sec']:.1f}s bbox={w['bbox']} fill={w['fill']}")
    if ya.get("candidates"):
        print(f"      (单帧候选 {len(ya['candidates'])} 处，多为球衣/地板误判："
              + ", ".join(f"{c['first_sec']:.1f}s" for c in ya["candidates"][:6]) + ")")
    cr = r["chinese_residue"]
    print(f"  chinese_residue {cr.get('status','?'):8s} ocr_frames={cr.get('frames_ocr',0)} groups={len(cr.get('groups',[]))}")
    for g in cr.get("groups", [])[:8]:
        print(f"      ! {g['text'][:18]} {g['first_sec']}-{g['last_sec']}s n={g['frames']} conf={g['max_confidence']:.2f}")
    for g in cr.get("single_char", [])[:5]:
        print(f"      ? 单字「{g['text'][:2]}」{g['first_sec']}-{g['last_sec']}s n={g['frames']} "
              f"conf={g['max_confidence']:.2f} bbox={g['bbox']}")
    print(f"  OVERALL         {r['overall'].upper()}")

    if r["overall"] == "fail":
        return 1
    if r["overall"] == "degraded":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
