#!/usr/bin/env python3
"""
ocr_video.py - 对采样帧进行 OCR 文字识别

引擎：RapidOCR（唯一默认，onnxruntime，初始化 ~2.6s）
      --engine paddleocr / tesseract 为显式排障选项，正常流程不启用。
      （三者在本机 conda 环境内均可运行，含沙箱内；paddleocr 初始化约 14s 故不作默认。）
输入：frames-index.json（或指定目录）
输出：work/ocr.json

职责：确定性 OCR，输出 text/language/bbox/timestamp/confidence。
不做分类判断（分类由 Agent + classify_text.md 完成）。

设计要点见 run_ocr() 内注释：**不做静默降级** —— 引擎不可用时明确报
degraded，而不是换一个弱引擎继续跑（这曾导致一处中文漏清理 + 假 PASS）。
"""

import argparse
import json
import os
import sys
from typing import List, Dict, Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    save_json, load_json, make_result, log,
    work_path, check_python_package, add_degradation,
    EXIT_DEGRADED, die_degraded,
)


def detect_chinese(text: str) -> bool:
    """简单判断文本是否含中文"""
    return any('\u4e00' <= c <= '\u9fff' for c in text)


# ============================================================
# 引擎实例缓存
# OCR 模型初始化开销极大（PaddleOCR 首次 10s+），
# 逐帧新建实例会让多帧扫描慢到不可用，必须复用单例。
# ============================================================
_ENGINE_CACHE: Dict[str, Any] = {}


def _get_rapidocr():
    if "rapidocr" not in _ENGINE_CACHE:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE_CACHE["rapidocr"] = RapidOCR()
    return _ENGINE_CACHE["rapidocr"]


def _get_paddleocr():
    if "paddleocr" not in _ENGINE_CACHE:
        from paddleocr import PaddleOCR
        _ENGINE_CACHE["paddleocr"] = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    return _ENGINE_CACHE["paddleocr"]


def ocr_with_rapidocr(image_path: str) -> List[Dict[str, Any]]:
    """
    用 RapidOCR (PP-OCR 的 ONNX 运行时) 识别单张图片。

    首选引擎：纯 onnxruntime 推理，不依赖 paddle 的 native so 库。
    实测（2026-09-22 无提权沙箱内）：初始化 ~2.6s，中文描边美术字 4/4 命中。

    注：PaddleOCR 在沙箱内**同样可以运行**（import / 实例化 / 推理均通过），
    它不作默认的真实原因是性能——初始化慢约 5 倍（~14s）、依赖体积大一个数量级。
    两者同属 PP-OCRv4，识别效果同源，置信度差异属噪声范围。
    """
    ocr = _get_rapidocr()
    result, _elapse = ocr(image_path)
    detections: List[Dict[str, Any]] = []
    if not result:
        return detections
    for item in result:
        bbox_pts, text, conf = item[0], item[1], float(item[2])
        if not text:
            continue
        xs = [p[0] for p in bbox_pts]
        ys = [p[1] for p in bbox_pts]
        detections.append({
            "text": text,
            "language": "zh" if detect_chinese(text) else "en",
            "bbox": {
                "x": int(min(xs)),
                "y": int(min(ys)),
                "w": int(max(xs) - min(xs)),
                "h": int(max(ys) - min(ys)),
            },
            "confidence": round(conf, 4),
        })
    return detections


def ocr_with_paddle(image_path: str) -> List[Dict[str, Any]]:
    """用 PaddleOCR 识别单张图片（复用单例，避免逐帧重建）"""
    ocr = _get_paddleocr()
    result = ocr.ocr(image_path, cls=True)
    detections = []
    if result and result[0]:
        for line in result[0]:
            bbox_pts = line[0]
            text = line[1][0]
            conf = float(line[1][1])
            xs = [p[0] for p in bbox_pts]
            ys = [p[1] for p in bbox_pts]
            detections.append({
                "text": text,
                "language": "zh" if detect_chinese(text) else "en",
                "bbox": {
                    "x": int(min(xs)),
                    "y": int(min(ys)),
                    "w": int(max(xs) - min(xs)),
                    "h": int(max(ys) - min(ys)),
                },
                "confidence": round(conf, 4),
            })
    return detections


def ocr_with_tesseract(image_path: str) -> List[Dict[str, Any]]:
    """用 Tesseract 识别单张图片"""
    import pytesseract
    from PIL import Image
    img = Image.open(image_path)
    data = pytesseract.image_to_data(img, lang="chi_sim+eng", output_type=pytesseract.Output.DICT)
    detections = []
    n_boxes = len(data["text"])
    for i in range(n_boxes):
        text = data["text"][i].strip()
        if not text or int(data["conf"][i]) < 0:
            continue
        conf = int(data["conf"][i]) / 100.0
        detections.append({
            "text": text,
            "language": "zh" if detect_chinese(text) else "en",
            "bbox": {
                "x": int(data["left"][i]),
                "y": int(data["top"][i]),
                "w": int(data["width"][i]),
                "h": int(data["height"][i]),
            },
            "confidence": round(conf, 4),
        })
    return detections


def run_ocr(frames: List[Dict], engine: str = "auto", min_confidence: float = 0.5) -> Dict[str, Any]:
    """对帧列表执行 OCR"""
    all_detections = []
    engine_used = None
    degraded = False
    warnings = []

    # 选择引擎
    #
    # 【设计决策 2026-09-22】auto 只认 rapidocr，且**不做静默降级**。
    #
    # 为什么删掉「降级到 tesseract」这条退路：
    #   这是一次真实事故的教训。当时 paddleocr 没装，auto 悄悄退到 tesseract，
    #   而 tesseract 对视频里的中文描边美术字召回率≈0 —— 结果 ocr.json 中
    #   C 视频的 4 处中文只报出 1 条单字误报，Agent 据此认为「没什么中文」，
    #   漏清了一处，而 QA 又复用同一份结论，最终给出假 PASS 交付出去。
    #   静默降级最危险之处在于：它让「能力缺失」看起来像「没有发现问题」。
    #   现在引擎不可用就明确报 degraded，让上层看得见。
    #
    # 为什么只用 rapidocr 一个：
    #   rapidocr 与 paddleocr 共用同一套 PP-OCRv4 模型，只是运行时不同
    #   （onnxruntime vs PaddlePaddle），实测准确率差异属噪声范围，
    #   同源引擎做交叉验证意义有限（盲区重合）。而 rapidocr 初始化快约 5 倍
    #   （~2.6s vs ~14s）、依赖体积小一个数量级，故定为唯一默认引擎。
    #   注意：选它**不是因为 paddleocr 在沙箱内跑不了**（那是 2026-09-22 的误判，
    #   复测证明沙箱内可正常 import/实例化/推理），纯粹是性能取舍。
    #   paddleocr / tesseract 仅保留为显式排障选项（--engine xxx）。
    if engine == "auto":
        engine = "rapidocr" if check_python_package("rapidocr_onnxruntime") else None

    if engine is None:
        warnings.append(
            "RapidOCR (rapidocr-onnxruntime) 不可用，OCR 已跳过 —— "
            "本结果不构成「画面无中文」的证据，禁止据此交付；"
            "安装：pip install rapidocr-onnxruntime"
        )
        result = make_result(
            status="degraded",
            confidence=0.0,
            ocr_engine="none",
            degraded=True,
            detections=[],
            warnings=warnings,
        )
        return result

    OCR_FUNCS = {
        "rapidocr": ocr_with_rapidocr,
        "paddleocr": ocr_with_paddle,
        "tesseract": ocr_with_tesseract,
    }
    OCR_BASE_CONF = {"rapidocr": 0.9, "paddleocr": 0.9, "tesseract": 0.75}
    ocr_func = OCR_FUNCS[engine]
    engine_used = f"{engine}_local"

    total = len(frames)
    for i, frame in enumerate(frames):
        ts = frame.get("timestamp_sec")
        if ts is None:
            continue
        img_path = frame.get("path")
        if not img_path or not os.path.exists(img_path):
            continue

        try:
            dets = ocr_func(img_path)
            for d in dets:
                if d["confidence"] >= min_confidence and d["bbox"]["h"] >= 8:
                    d["frame_id"] = frame.get("frame_id")
                    d["timestamp_sec"] = round(ts, 3)
                    d["pass"] = frame.get("pass", "unknown")
                    all_detections.append(d)
        except Exception as e:
            warnings.append(f"OCR failed for frame {frame.get('frame_id')}: {str(e)[:200]}")
            continue

        if (i + 1) % 50 == 0:
            log.info(f"  OCR progress: {i+1}/{total} frames, {len(all_detections)} detections")

    result = make_result(
        status="ok" if not warnings else "ok_with_warnings",
        confidence=OCR_BASE_CONF[engine],
        ocr_engine=engine_used,
        degraded=degraded,
        detections=all_detections,
        total_frames=total,
        frames_with_text=len(set(d["frame_id"] for d in all_detections)),
        warnings=warnings,
    )
    return result


def main():
    ap = argparse.ArgumentParser(description="OCR text detection on sampled frames")
    ap.add_argument("--work-dir", required=True, help="Working directory")
    ap.add_argument("--frames-index", help="Path to frames-index.json (default: work/frames-index.json)")
    ap.add_argument("--engine", default="auto", choices=["auto", "rapidocr", "paddleocr", "tesseract"],
                    help="OCR engine（auto=只用 rapidocr，不降级；paddleocr/tesseract 仅排障用）")
    ap.add_argument("--min-confidence", type=float, default=0.5, help="Minimum OCR confidence")
    ap.add_argument("--pass", dest="pass_filter", default=None, help="Only OCR specific pass (coarse/scene/dense)")
    args = ap.parse_args()

    index_path = args.frames_index or work_path(args.work_dir, "frames-index.json")
    frames = load_json(index_path, [])
    if not frames:
        log.error(f"No frames found in {index_path}")
        sys.exit(EXIT_DEGRADED)

    if args.pass_filter:
        frames = [f for f in frames if f.get("pass") == args.pass_filter]
        log.info(f"Filtered to {len(frames)} frames (pass={args.pass_filter})")

    log.info(f"Running OCR on {len(frames)} frames (engine={args.engine})")
    result = run_ocr(frames, args.engine, args.min_confidence)

    # 合并既有 ocr.json（多 pass 应合并而非覆盖，2026-09-26 修复）
    prev_path = work_path(args.work_dir, "ocr.json")
    prev = load_json(prev_path, None)
    if prev and isinstance(prev, dict) and prev.get("detections"):
        dets_now = result.setdefault("detections", [])
        known = set()
        for d in dets_now:
            known.add((d.get("frame_id"), d.get("text"), json.dumps(d.get("bbox"), sort_keys=True)))
        added = 0
        for d in prev["detections"]:
            key = (d.get("frame_id"), d.get("text"), json.dumps(d.get("bbox"), sort_keys=True))
            if key not in known:
                dets_now.append(d)
                added += 1
        if added:
            result["frames_with_text"] = len(set(d["frame_id"] for d in dets_now if d.get("frame_id") is not None))
            log.info(f"  Merged {added} detections from previous ocr.json (total {len(dets_now)})")

    out_path = work_path(args.work_dir, "ocr.json")
    save_json(out_path, result)

    log.info(f"OCR complete: {len(result['detections'])} detections -> {out_path}")
    log.info(f"  Engine: {result['ocr_engine']}, Frames with text: {result.get('frames_with_text', 0)}")

    if result["degraded"]:
        log.warning("OCR degraded - text processing will be limited")
        for w in result["warnings"]:
            log.warning(f"  {w}")


if __name__ == "__main__":
    main()
