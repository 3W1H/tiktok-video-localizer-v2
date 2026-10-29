#!/usr/bin/env python3
"""Pick the best cover frame instead of taking the first sharp one.

Why: a wide court shot is full of crowd, and no amount of post-processing
rescues it -- the user sees "花". A close-up of the subject is what makes a
cover readable, so score every scene frame by

    face_area * 1000 - clutter * 420 + min(lap / 300, 1.2) * 60

where face_area is the summed Haar frontal-face box area (fraction of the
frame), clutter is the Canny edge density of the top 45% of the frame (the
crowd band: busy = bad) and lap is the Laplacian variance (too soft = bad).

Usage:
  # rank the scene frames
  python pick_cover_frame.py --work-dir work

  # ranked contact sheet to eyeball the shortlist
  python pick_cover_frame.py --work-dir work --contact /tmp/scene_ranked.png

  # pull the top frame from the source, wipe the burned-in text, save a base
  python pick_cover_frame.py --work-dir work --extract 1 \
      --out work/cover-base.png \
      --inpaint 317,825,86,35 --inpaint 222,916,278,38 --inpaint 176,1038,367,41

  # or let OCR find the text boxes on that frame and wipe them
  python pick_cover_frame.py --work-dir work --extract 1 --out work/cover-base.png --ocr-inpaint

Feed the result to make_cover.py, e.g.
  python make_cover.py --video work/cover-base.png --time 0 --out cover.jpg \
      --headline "ALLEN IVERSON" --stat "48 POINTS" --tagline "SHOCK THE LAKERS" \
      --logo "Hoop Dynasty Daily" --crop-top 240 --crop-bottom 1000
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw

FFMPEG = shutil.which("ffmpeg") or os.path.expanduser("~/.local/bin/ffmpeg")

# text that belongs to the jersey/scoreboard -- never wipe these
OCR_KEEP = ("SHERS", "SIXERS", "IVERSON", "LAKERS", "PHILA")

_EYE_CAS = None


def _eye_cascade():
    """二级验证用的眼睛级联（惰性加载、模块级缓存，避免每帧重读 XML）。"""
    global _EYE_CAS
    if _EYE_CAS is None:
        _EYE_CAS = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_eye.xml")
    return _EYE_CAS


def _verified_faces(gray, faces):
    """Haar 正脸检测的二级验证：脸部 ROI 内必须至少检出一只眼睛。

    背景（2026-10-28 国王封面实测）：单级 frontalface 级联在模糊/背影画面上
    误报严重 —— 背影镜头（WEBBER 4 球衣）被判 face_area≈0.23 的高分，
    自动选帧精准地选中了一张**没有脸**的封面。眼部复核能杀掉绝大多数
    背影/花纹误报，代价极小（ROI 很小）。
    """
    eye = _eye_cascade()
    out = []
    for (x, y, fw, fh) in faces:
        roi = gray[y:y + fh, x:x + fw]
        if roi.size == 0:
            continue
        eyes = eye.detectMultiScale(roi, 1.1, 4, minSize=(max(8, fh // 6),
                                                         max(8, fh // 6)))
        if len(eyes) >= 1:
            out.append((x, y, fw, fh))
    return out


def score_frame(path):
    img = cv2.imread(path)
    if img is None:
        return None
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cas = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cas.detectMultiScale(gray, 1.1, 5, minSize=(36, 36))
    faces = _verified_faces(gray, faces)
    face = sum(int(fw) * int(fh) for (_, _, fw, fh) in faces) / float(w * h)
    lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    top = gray[:int(h * 0.45)]
    clutter = float(cv2.Canny(top, 60, 160).mean()) / 255.0
    return dict(path=path, n_faces=len(faces), face=face, lap=lap,
                clutter=clutter,
                score=face * 1000 - clutter * 420 + min(lap / 300.0, 1.2) * 60)


def ranked_frames(work_dir):
    idx_path = os.path.join(work_dir, "frames-index.json")
    entries = json.load(open(idx_path, encoding="utf-8"))
    scene = [e for e in entries if e.get("pass") == "scene"]
    if not scene:
        scene = [e for e in entries
                 if os.path.basename(e.get("path", "")).startswith("s_")]
    rows = []
    for e in scene:
        p = e.get("path")
        if not p or not os.path.exists(p):
            continue
        r = score_frame(p)
        if r:
            r["timestamp_sec"] = e.get("timestamp_sec")
            r["frame_id"] = e.get("frame_id")
            rows.append(r)
    rows.sort(key=lambda r: -r["score"])
    return rows


def contact_sheet(rows, out, cw=180, ch=320, cols=7):
    n = len(rows)
    g = Image.new("RGB", (cols * cw, ((n + cols - 1) // cols) * (ch + 18)),
                  (18, 18, 18))
    d = ImageDraw.Draw(g)
    for i, r in enumerate(rows):
        im = Image.open(r["path"]).convert("RGB").resize((cw, ch), Image.LANCZOS)
        x, y = (i % cols) * cw, (i // cols) * (ch + 18)
        g.paste(im, (x, y))
        d.text((x + 4, y + ch + 3),
               f'{r["frame_id"][:9]} {r["timestamp_sec"]:.1f}s s={r["score"]:.0f}',
               fill=(230, 230, 230))
    g.save(out)
    return g.size


def ocr_boxes(path):
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        print("rapidocr_onnxruntime not available -- pass --inpaint boxes instead",
              file=sys.stderr)
        return []
    res, _ = RapidOCR()(np.array(Image.open(path).convert("RGB")))
    boxes = []
    for box, text, _conf in (res or []):
        t = (text or "").upper().replace(" ", "")
        if any(k in t for k in OCR_KEEP):
            continue                      # jersey / scoreboard text, keep it
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        boxes.append((int(min(xs)), int(min(ys)),
                      int(max(xs) - min(xs)), int(max(ys) - min(ys))))
    return boxes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--top", type=int, default=10, help="rows to print")
    ap.add_argument("--contact", default="", help="write a ranked contact sheet here")
    ap.add_argument("--extract", type=int, default=0,
                    help="1-based rank to pull from the source video")
    ap.add_argument("--source", default="",
                    help="source video (default: work/probe.json 'path')")
    ap.add_argument("--out", default="", help="output base image for --extract")
    ap.add_argument("--inpaint", action="append", default=[],
                    metavar="X,Y,W,H", help="repeatable; boxes to wipe")
    ap.add_argument("--ocr-inpaint", action="store_true",
                    help="auto-detect the burned-in text boxes with RapidOCR")
    ap.add_argument("--pad", type=int, default=5)
    ap.add_argument("--radius", type=int, default=12)
    args = ap.parse_args()

    rows = ranked_frames(args.work_dir)
    if not rows:
        print("no scene frames found -- run the frames step first", file=sys.stderr)
        return 1

    print(f"{len(rows)} scene frames ranked (best first):")
    for r in rows[:args.top]:
        print(f'  {r["frame_id"]:8s} t={r["timestamp_sec"]:7.2f}s  '
              f'faces={r["n_faces"]} faceArea={r["face"]*100:5.2f}%  '
              f'clutter={r["clutter"]:.3f}  lap={r["lap"]:6.0f}  score={r["score"]:7.1f}')
    print("worst:")
    for r in rows[-3:]:
        print(f'  {r["frame_id"]:8s} t={r["timestamp_sec"]:7.2f}s  '
              f'clutter={r["clutter"]:.3f}  score={r["score"]:7.1f}')

    if args.contact:
        print("contact sheet ->", args.contact, contact_sheet(rows, args.contact))

    if not args.extract:
        return 0
    if not args.out:
        print("--extract needs --out", file=sys.stderr)
        return 1

    pick = rows[args.extract - 1]
    src = args.source
    if not src:
        probe = os.path.join(args.work_dir, "probe.json")
        src = json.load(open(probe, encoding="utf-8"))["path"]
    tmp = "/tmp/pick_cover_frame_raw.png"
    subprocess.run([FFMPEG, "-y", "-ss", str(pick["timestamp_sec"]), "-i", src,
                    "-frames:v", "1", tmp],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    img = cv2.imread(tmp)
    H, W = img.shape[:2]
    boxes = []
    for spec in args.inpaint:
        x, y, w, h = (int(v) for v in spec.split(","))
        boxes.append((x, y, w, h))
    if args.ocr_inpaint:
        boxes += ocr_boxes(tmp)
    if boxes:
        mask = np.zeros((H, W), np.uint8)
        for (x, y, w, h) in boxes:
            mask[max(0, y - args.pad):min(H, y + h + args.pad),
                 max(0, x - args.pad):min(W, x + w + args.pad)] = 255
        img = cv2.inpaint(img, mask, args.radius, cv2.INPAINT_TELEA)
        print(f"wiped {len(boxes)} box(es): {boxes}")
    cv2.imwrite(args.out, img)
    print(f'extracted rank {args.extract} ({pick["frame_id"]} @ {pick["timestamp_sec"]:.2f}s)'
          f' -> {args.out}  [{W}x{H}]')
    return 0


if __name__ == "__main__":
    sys.exit(main())
