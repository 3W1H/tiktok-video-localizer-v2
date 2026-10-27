#!/usr/bin/env python3
"""Expand title/watermark bboxes to cover the tapered coloured badge ends,
WITHOUT running away into the footage.

The orange title banner is wider than the OCR text box, so inpaint left its two
tapered ends behind (they read as arrows). We must grow the box to the colour
extent -- but a naive full-width colour scan also picks up arena/footage colour
and blows the box up to the whole frame width (observed on the 720x1280 clips).

So: anchor on the OCR text box (from text-tracks.json) and search for colour
only within +/- LIMIT px of it, then cap the result to +/- LIMIT too.
"""
import cv2, glob, json, os
import numpy as np

DL = os.environ.get("TTL_LOCALIZE_ROOT", os.path.expanduser("~/Downloads/tiktok"))
LIMIT = 80          # max px to grow beyond the OCR text box, each side
PAD = 4             # baseline pad around the OCR box


def colour_extent(imgs, y0, y1, x0, x1):
    ax0, ay0, ax1, ay1 = [], [], [], []
    for im in imgs:
        h, wd = im.shape[:2]
        a, b = max(0, y0), min(h, y1)
        c, d = max(0, x0), min(wd, x1)
        if b - a < 2 or d - c < 2:
            continue
        sub = cv2.cvtColor(im[a:b, c:d], cv2.COLOR_BGR2HSV)
        m = (sub[:, :, 1] > 110) & (sub[:, :, 2] > 120)
        ys, xs = np.where(m)
        if not len(xs):
            continue
        ax0.append(int(xs.min()) + c); ax1.append(int(xs.max()) + c)
        ay0.append(int(ys.min()) + a); ay1.append(int(ys.max()) + a)
    if not ax0:
        return None
    return min(ax0), min(ay0), max(ax1), max(ay1)


def main():
    for w in sorted(glob.glob(DL + "/*/work")):
        name = os.path.basename(os.path.dirname(w))
        plan_p = os.path.join(w, "localization-plan.json")
        tt_p = os.path.join(w, "text-tracks.json")
        if not (os.path.exists(plan_p) and os.path.exists(tt_p)):
            continue
        d = json.load(open(plan_p, encoding="utf-8"))
        tt = json.load(open(tt_p, encoding="utf-8"))
        ocr = {}
        for t in tt.get("tracks", []):
            if t.get("language") == "zh" and t.get("bbox"):
                ocr.setdefault(t["text"], t["bbox"])

        frames = sorted(glob.glob(os.path.join(w, "frames", "coarse", "c_*.png")))[:12]
        imgs = [cv2.imread(f) for f in frames]
        imgs = [i for i in imgs if i is not None]
        if not imgs:
            continue
        W, H = imgs[0].shape[1], imgs[0].shape[0]

        out = []
        for it in d.get("items", []):
            if it.get("category") != "title":
                continue
            src = it.get("source_text")
            ob = ocr.get(src)
            if not ob:
                continue
            bx = max(0, ob["x"] - PAD); by = max(0, ob["y"] - PAD)
            bw = min(W, ob["x"] + ob["w"] + PAD) - bx
            bh = min(H, ob["y"] + ob["h"] + PAD) - by
            ext = colour_extent(imgs,
                                by - 24, by + bh + 24,
                                bx - LIMIT, bx + bw + LIMIT)
            old = it["bbox"]
            if ext:
                ex0, ey0, ex1, ey1 = ext
                # A colour blob that spans BOTH frame edges is the footage, not
                # the badge (happens on the 720x1280 clips whose title sits on
                # the picture) -> reject and fall back to the OCR box.
                if ex0 <= 12 and ex1 >= W - 12:
                    ext = None
            if ext:
                ex0, ey0, ex1, ey1 = ext
                nx0 = max(bx - LIMIT, ex0); nx1 = min(bx + bw + LIMIT, ex1)
                ny0 = max(by - LIMIT, ey0); ny1 = min(by + bh + LIMIT, ey1)
                nx0 = min(nx0, bx); nx1 = max(nx1, bx + bw)
                ny0 = min(ny0, by); ny1 = max(ny1, by + bh)
                it["bbox"] = {"x": nx0, "y": ny0, "w": nx1 - nx0, "h": ny1 - ny0}
            else:
                it["bbox"] = {"x": bx, "y": by, "w": bw, "h": bh}
            out.append(f"{it['item_id']} {old} -> {it['bbox']}")
        if out:
            json.dump(d, open(plan_p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"  {name[:28]:30s} " + " | ".join(out))
        else:
            print(f"  {name[:28]:30s} (no title items)")


if __name__ == "__main__":
    main()
