#!/usr/bin/env python3
"""Spec assertions for the cover pipeline.

Why this exists: the cover rules in SKILL.md 8 drifted from the implementation
TWICE in one day (文档写着 Impact、脚本硬编码 Arial Black；文档写着渐变外发光、
脚本只画纯金平涂). Both were found by the user looking at the output, not by any
check. These assertions turn each of those spec statements into something that
fails loudly instead of quietly.

Run either way:

    /Users/jason/opt/miniconda3/bin/python tests/test_cover_spec.py
    python -m pytest tests/test_cover_spec.py -q      # if pytest is available

No external fixtures: the frame is synthesised, so this never depends on the
user's video files.
"""
from __future__ import annotations

import json
import os
import re
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

from cover_engine import (  # noqa: E402
    GOLD, STACK_BRAND_LEAD, CoverSpec, letter_cover, profile_cover_style,
    render_cover, resolve,
)

PROFILES = ("nba", "sports", "generic")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def synthetic_frame(w=720, h=1280):
    """A gradient frame with a soft bright blob -- enough to exercise the whole
    composition path without touching any real video."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.sqrt(((xx - w * 0.5) / (w * 0.35)) ** 2
                + ((yy - h * 0.42) / (h * 0.28)) ** 2)
    blob = np.clip(1.25 - r, 0, 1)
    base = np.zeros((h, w, 3), np.float32)
    base[..., 0] = 40 + 150 * blob
    base[..., 1] = 30 + 120 * blob
    base[..., 2] = 45 + 60 * (1 - blob)
    base[..., :] += (yy / h)[..., None] * 35
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))


def spec_for(profile, **over):
    style = profile_cover_style(profile)
    kw = dict(headline="ALLEN IVERSON", sub="2001 NBA FINALS - GAME 1",
              stat="48 POINTS", tagline="SIXERS SHOCK THE LAKERS",
              logo=style.get("brand_text") or "", style=style)
    kw.update(over)
    return CoverSpec(**kw)


def is_yellow(rgb):
    """The rule qa_video.check_cover applies."""
    r, g, b = rgb[0], rgb[1], rgb[2]
    return r >= 230 and g >= 180 and b <= 80 and g > b


def boxes_of(meta):
    b = list(meta.get("text_regions") or [])
    if meta.get("brand_region"):
        b.append(meta["brand_region"])
    return b


def brand_gap(gaps):
    """The gap that precedes the signature row, whatever precedes it."""
    for k, g in gaps.items():
        if k.endswith("->logo"):
            return g
    raise AssertionError(f"no row precedes the signature: {sorted(gaps)}")


def out_of_band(meta):
    z = meta["safe_text_zone"]
    y0, y1 = z["y"], z["y"] + z["h"]
    return [b.get("text", "?") for b in boxes_of(meta)
            if b["y"] < y0 or b["y"] + b["h"] > y1]


# --------------------------------------------------------------------------
# 8.2 -- sizes / font / colour come from the profile, never hardcoded
# --------------------------------------------------------------------------
def test_sizes_come_from_the_profile():
    for name in PROFILES:
        style = profile_cover_style(name)
        res = resolve(CoverSpec(headline="TEST", style=style))
        if style.get("font_size"):
            assert res.head_size == int(style["font_size"]), (
                f"{name}: headline size {res.head_size} != profile "
                f"font_size {style['font_size']} (hardcoded?)")
        if style.get("brand_size"):
            assert res.logo_size == int(style["brand_size"]), (
                f"{name}: brand size {res.logo_size} != profile brand_size "
                f"{style['brand_size']} (hardcoded?)")
        if style.get("brand_stroke_width"):
            assert res.brand_stroke == int(style["brand_stroke_width"]), (
                f"{name}: brand stroke {res.brand_stroke} != profile "
                f"brand_stroke_width {style['brand_stroke_width']}")
        print(f"  {name}: headline {res.head_size}, brand {res.logo_size}, "
              f"brand_stroke {res.brand_stroke}, "
              f"safe {res.safe_top}/{res.safe_bottom}, picture {res.picture}")


def test_rendered_sizes_match_the_profile():
    """End-to-end: what actually got drawn equals what the profile says."""
    for name in PROFILES:
        style = profile_cover_style(name)
        spec = spec_for(name)
        meta = render_cover(synthetic_frame(), spec).meta
        if style.get("font_size"):
            # `block_scale` scales the title block (2026-10-15). The brand row
            # deliberately does NOT participate -- it is a signature, not a row.
            scale = float(style.get("block_scale") or 1.0)
            want = max(40, int(round(int(style["font_size"]) * scale)))
            assert meta["sizes"]["headline"] == want, (
                f"{name}: drew headline {meta['sizes']['headline']}px, "
                f"profile says {style['font_size']} x block_scale {scale} = "
                f"{want}px")
        if style.get("brand_size"):
            assert meta["sizes"]["brand"] == int(style["brand_size"]), (
                f"{name}: drew brand {meta['sizes']['brand']}px, "
                f"profile says {style['brand_size']}")
        print(f"  {name}: rendered {meta['sizes']}")


def test_font_resolves_through_the_chain():
    """font_chain must be honoured -- hardcoding Arial Black was bug #1."""
    style = profile_cover_style("nba")
    res = resolve(CoverSpec(headline="X", style=style))
    assert os.path.exists(res.head_font), res.head_font
    chain = [str(c).lower() for c in (style.get("font_chain") or [])]
    used = os.path.basename(res.head_font).lower()
    print(f"  chain={style.get('font_chain')} -> {used}")
    if "impact" in chain:
        assert "impact" in used, (
            f"nba font_chain has Impact but the resolved face is {res.head_font} "
            "-- the chain is being ignored again (SKILL.md 8.2)")
    if "anton" in chain:
        # Anton is listed first; it wins only if it is actually installed
        anton = resolve_font_chain_probe("anton")
        assert ("anton" in used) == bool(anton), (
            f"Anton installed={bool(anton)} but resolved face is {res.head_font}")


def resolve_font_chain_probe(name):
    """Is a chain entry actually installed on this machine?"""
    from common import SYSTEM_FONTS
    paths = SYSTEM_FONTS.get(name.lower(), [])
    return next((p for p in paths if os.path.exists(os.path.expanduser(p))), None)


def test_fill_color_is_tiktok_yellow():
    for name in PROFILES:
        style = profile_cover_style(name)
        effective = style.get("fill_color") or list(GOLD)
        assert is_yellow(effective), (
            f"{name}: fill_color {effective} is not TikTok yellow #FFDC00 "
            "(SKILL.md 8.2 forbids white)")
        print(f"  {name}: fill_color {effective} -> yellow OK")


# --------------------------------------------------------------------------
# 8.3 -- the safe band
# --------------------------------------------------------------------------
def test_all_text_inside_the_safe_band():
    for name in PROFILES:
        spec = spec_for(name)
        meta = render_cover(synthetic_frame(), spec).meta
        bad = out_of_band(meta)
        assert not bad, f"{name}: text outside the safe band: {bad}"
        assert meta["safe_ok"], f"{name}: safe_ok is False"
        z = meta["safe_text_zone"]
        print(f"  {name}: composition {meta['composition']} inside "
              f"{z['y']}-{z['y'] + z['h']}, regions {len(boxes_of(meta))}")


def test_safe_band_matches_the_profile():
    for name in PROFILES:
        style = profile_cover_style(name)
        meta = render_cover(synthetic_frame(), spec_for(name)).meta
        s_top = int(1920 * float(style.get("safe_zone_top_pct") or 0.22))
        s_bot = int(1920 * (1.0 - float(style.get("safe_zone_bottom_pct") or 0.22)))
        assert meta["safe_band"]["top"] == s_top, (
            f"{name}: band top {meta['safe_band']['top']} != {s_top}")
        assert meta["safe_band"]["bottom"] == s_bot, (
            f"{name}: band bottom {meta['safe_band']['bottom']} != {s_bot}")
        print(f"  {name}: band {s_top}-{s_bot}")


def test_brand_sits_in_the_lower_half():
    """8.1: the brand mark belongs at the BOTTOM. It was once drawn at the top.

    2026-10-15, third pass: the block became TOP-anchored (`stack_anchor: top`)
    so the whole text block clears the profile-grid edge. That lifts the brand
    ink's CENTRE to about the band midpoint, so the invariant is stated on the
    ink's BOTTOM edge -- the signature must still land in the lower half -- plus
    a floor that stops it drifting up into the top third with the title block.
    """
    for name in PROFILES:
        if not profile_cover_style(name).get("brand_text"):
            print(f"  {name}: no brand_text configured, skipped")
            continue
        meta = render_cover(synthetic_frame(), spec_for(name)).meta
        br = meta.get("brand_region")
        assert br, f"{name}: brand region missing though brand_text is configured"
        z = meta["safe_text_zone"]
        mid = z["y"] + z["h"] / 2
        assert br["y"] + br["h"] > mid, (
            f"{name}: brand ink bottom {br['y'] + br['h']} is above the band "
            f"midpoint {mid} -- it must stay in the lower half (SKILL.md 8.1)")
        floor = z["y"] + z["h"] * 0.35
        assert br["y"] + br["h"] / 2 > floor, (
            f"{name}: brand centre {br['y'] + br['h'] / 2} crept above {floor:.0f} "
            "-- the signature is drifting up with the title block")
        print(f"  {name}: brand ink bottom {br['y'] + br['h']:.0f} > mid {mid:.0f}")


def test_stack_layout_keeps_the_copy_together():
    """8.4: nba renders `stack` -- the copy rows stay together, and BOTH ends of
    the band get a real margin.

    History (2026-10-15), four passes in one day:
      1. `safe` split the copy into two groups and left the brand **11px** off the
         bottom edge -- 「封面上的文字怎么都变成了上下呢，下方的 logo 和文字
         混在一起」.
      2. `stack` in one bottom-anchored block -- 「标题太靠下了」.
      3. block lifted to the head of the band -- 「框里中间的小字是什么」.
      4. copy heads the band, the signature foots it (`stack_anchor: split`) --
         「logo 怎么往上提了呢，还是要标题在上，logo 在下呢」.
    So the assertions are now anchor-aware: the copy rows always keep the tight
    rhythm, and the anchor decides where the mark goes.
    """
    name = "nba"
    style = profile_cover_style(name)
    assert style.get("layout") == "stack", (
        f"{name}: expected layout 'stack' (SKILL.md 8.4), got "
        f"{style.get('layout')!r}")
    meta = render_cover(synthetic_frame(), spec_for(name)).meta
    assert meta["layout"] == "stack", (
        f"meta reports layout {meta['layout']!r} -- stack fell through to safe")
    rows = meta["text_regions"]
    kinds = [r["kind"] for r in rows]
    drop = set(style.get("drop_rows") or ())
    want = [k for k in ("headline", "sub", "stat", "tag", "logo")
            if k not in drop]
    assert kinds == want, (
        f"the single block must read {'/'.join(want)} (drop_rows={sorted(drop)}), "
        f"got {kinds}")
    for r in rows:
        assert abs(r["x"] + r["w"] / 2 - 540) <= 2, (
            f"{r['kind']} is off-centre: cx={r['x'] + r['w'] / 2}")
    z = meta["safe_text_zone"]
    assert meta["safe_ok"], (
        f"the block must stay in the band: composition {meta['composition']} "
        f"vs {z['y']}-{z['y'] + z['h']}")
    clear = z["y"] + z["h"] - meta["composition"]["bottom"]
    assert clear >= 60, (
        f"block bottom is only {clear}px above the band bottom -- the brand "
        "must not hug the profile-grid edge (the 2026-10-15 bug left 11px)")
    # Two gap rules, because the block has two kinds of row.
    #
    # Content rows (headline / sub / stat / tag) keep the tight block rhythm:
    # no gap there may open up wide enough to read as a second group. The old
    # rule keyed on the sub->stat break being the widest, which stopped applying
    # once `drop_rows: [sub]` removed that row (2026-10-15, third pass).
    #
    # The signature is the ONE deliberate exception: it is not a content row but
    # a mark, so the `->logo` gap is governed by the anchor, not by the rhythm.
    gaps = {}
    for a, b in zip(rows, rows[1:]):
        gaps[f"{a['kind']}->{b['kind']}"] = b["y"] - (a["y"] + a["h"])
    ceiling = z["h"] * 0.09
    anchor = style.get("stack_anchor") or "bottom"
    for k, g in gaps.items():
        if k.endswith("->logo") and anchor == "split":
            continue                      # pinned to the band foot, checked below
        assert g <= ceiling, (
            f"{k} gap is {g}px (ceiling {ceiling:.0f}px) -- wide enough to read "
            "as a second group again")
    inset = z["h"] * float(style.get("stack_bottom_inset_pct") or 0.075)
    if anchor == "split":
        # copy heads the band, mark foots it, same margin at both ends
        logo = next(r for r in rows if r["kind"] == "logo")
        assert abs(rows[0]["y"] - (z["y"] + inset)) <= 20, (
            f"the copy starts {rows[0]['y'] - z['y']}px below the band top but "
            f"the profile inset says {inset:.0f}px")
        assert abs((logo["y"] + logo["h"]) - (z["y"] + z["h"] - inset)) <= 20, (
            f"the signature ink ends {z['y'] + z['h'] - logo['y'] - logo['h']}px "
            f"above the band bottom but the profile inset says {inset:.0f}px -- "
            "with split the mark must be a footer, not the copy's last line")
    else:
        brand_lead = z["h"] * STACK_BRAND_LEAD
        assert abs(brand_gap(gaps) - brand_lead) <= 15, (
            f"the lead above the signature is {brand_gap(gaps)}px but the engine "
            f"says {brand_lead:.0f}px -- the signature must sit the same distance "
            "below the block on every cover, whatever the stat wraps to")
    print(f"  {name}: anchor={anchor} {meta['composition']}, gaps {gaps}, "
          f"clear {clear}px -> OK")


def test_safe_layout_still_renders_two_groups():
    """The old `safe` layout must keep working -- it is the documented fallback
    (`generate_cover.py --layout safe`) for covers the user wants two-group."""
    style = profile_cover_style("nba")
    meta = render_cover(synthetic_frame(),
                        spec_for("nba", layout="safe")).meta
    rows = meta["text_regions"]
    kinds = [r["kind"] for r in rows]
    assert kinds == ["headline", "sub", "stat", "tag", "logo"], kinds
    # first row is the floating title, not a stacked one
    assert rows[0]["y"] < rows[2]["y"], "safe layout must put the title on top"
    assert meta["safe_ok"], f"safe layout overflowed: {meta['composition']}"
    print(f"  safe: composition {meta['composition']} (fallback OK)")


def test_safe_band_is_the_platform_intersection():
    """8.3: the band is a PLATFORM constraint, not a per-profile style choice.

    sports.yaml / generic.yaml shipped 0.10/0.15 (= y192-1632), which is
    outside even the 3:4 grid window (y240-1680) -- covers made with those
    profiles still lost their title and logo. Every profile must use the
    intersection 0.22/0.22.
    """
    for name in PROFILES:
        style = profile_cover_style(name)
        top = float(style.get("safe_zone_top_pct") or 0.22)
        bot = float(style.get("safe_zone_bottom_pct") or 0.22)
        assert abs(top - 0.22) < 1e-9 and abs(bot - 0.22) < 1e-9, (
            f"{name}: safe_zone {top}/{bot} != 0.22/0.22 -- anything wider than "
            "the 3:4 grid window (240-1680) gets cropped by TikTok")
        print(f"  {name}: safe_zone {top}/{bot} -> band "
              f"{int(1920 * top)}-{int(1920 * (1 - bot))}")


def test_letter_cover_also_obeys_the_band():
    """The --cover-image path must letter inside the band too (it used to draw
    at 4% of the height, which the profile grid crops away)."""
    spec = spec_for("nba")
    meta = letter_cover(synthetic_frame(), spec).meta
    assert not out_of_band(meta), out_of_band(meta)
    assert meta["safe_ok"]
    print(f"  letter layout composition {meta['composition']}, "
          f"regions {len(boxes_of(meta))}")


def test_letter_cover_respects_no_resize():
    spec = spec_for("nba")
    spec.resize_to_canvas = False
    meta = letter_cover(synthetic_frame(640, 480), spec).meta
    assert meta["canvas"] == {"width": 640, "height": 480}, meta["canvas"]
    print(f"  no-resize keeps the source canvas: {meta['canvas']}")


# --------------------------------------------------------------------------
# 8.1 -- long titles wrap instead of shrinking; brand_text always renders
# --------------------------------------------------------------------------
def test_long_title_wraps_without_shrinking():
    style = profile_cover_style("nba")
    spec = spec_for("nba", headline="KOBE BRYANT 81 POINT GAME", sub="", stat="81 POINTS")
    meta = render_cover(synthetic_frame(), spec).meta
    scale = float(style.get("block_scale") or 1.0)
    want = max(40, int(round(int(style["font_size"]) * scale)))
    assert meta["sizes"]["headline"] == want, (
        f"long title shrank to {meta['sizes']['headline']}px -- it should wrap "
        f"and keep {want}px (font_size x block_scale; SKILL.md 8.2)")
    assert len(meta["headline_lines"]) >= 2, (
        "a title this long must actually wrap, not fit on one line")
    print(f"  '{' / '.join(meta['headline_lines'])}' at {meta['sizes']['headline']}px")


def test_brand_text_is_rendered_when_configured():
    for name in PROFILES:
        style = profile_cover_style(name)
        if not style.get("brand_text"):
            print(f"  {name}: no brand_text configured, skipped")
            continue
        spec = CoverSpec(headline="TEST", style=style, logo=style["brand_text"])
        meta = render_cover(synthetic_frame(), spec).meta
        assert meta["brand_text_rendered"], f"{name}: brand_text not rendered"
        assert meta["brand_region"], f"{name}: no brand_region recorded"
        print(f"  {name}: '{meta['brand_text']}' rendered at {meta['brand_region']}")


# --------------------------------------------------------------------------
# 8.0 -- one engine, thin CLIs (this is the assertion that stops the split)
# --------------------------------------------------------------------------
def test_clis_do_not_reimplement_rendering():
    """make_cover.py / generate_cover.py must delegate to cover_engine.py.

    Two independent composers is exactly what produced "手调的封面和流水线出的
    封面不是一张图" (2026-09-27). If someone adds ImageDraw back to a CLI, this
    fails.
    """
    for cli in ("make_cover.py", "generate_cover.py"):
        src = open(os.path.join(SCRIPTS, cli), encoding="utf-8").read()
        code = re.sub(r'""".*?"""', "", src, flags=re.S)      # drop docstrings
        code = re.sub(r"#.*", "", code)
        code = re.sub(r'f?"[^"]*"', '""', code)               # drop strings/help
        assert "ImageDraw" not in code, (
            f"{cli} draws on its own again -- rendering belongs in "
            "cover_engine.py")
        assert "ImageFont" not in code, (
            f"{cli} loads fonts on its own again -- font resolution belongs in "
            "cover_engine.py")
        assert "cover_engine" in src, f"{cli} does not import cover_engine"
        print(f"  {cli}: delegates, no drawing code")


def test_engine_is_the_only_composer():
    src = open(os.path.join(SCRIPTS, "cover_engine.py"), encoding="utf-8").read()
    for fn in ("def compose", "def render_cover", "def letter_cover",
               "def resolve", "def draw_brand_lockup", "def format_report"):
        assert fn in src, f"cover_engine.py lost {fn}()"
    print("  cover_engine.py exposes compose/render_cover/letter_cover/"
          "resolve/draw_brand_lockup/format_report")


def test_qa_can_read_the_plan_render_block():
    """qa_video.check_cover reads these keys; a rename would silently disable
    the checks (it uses .get() everywhere)."""
    import argparse
    from generate_cover import build_spec
    args = argparse.Namespace(sub=None, stat=None, tagline=None)
    spec = build_spec("TEST", profile_cover_style("nba"), {}, args)
    meta = render_cover(synthetic_frame(), spec).meta
    meta.update({"faces_detected": 0, "face_areas": [], "frame_cleaned_regions": 0})
    needed = ["faces_detected", "face_areas", "text_regions", "safe_text_zone",
              "brand_text_rendered", "brand_region", "frame_cleaned_regions",
              "picture_src"]
    for k in needed:
        assert k in meta, (
            f"cover-plan render block is missing '{k}' -- qa_video.check_cover "
            "reads it with .get() and would silently pass")
    # and it must survive a JSON round trip (dict keys, not tuples)
    json.loads(json.dumps(meta))
    print(f"  render block carries all {len(needed)} keys qa_video needs")


def test_face_mapping_lands_inside_the_picture():
    from cover_engine import map_boxes_to_canvas
    meta = render_cover(synthetic_frame(), spec_for("nba")).meta
    src = meta["source"]
    boxes = [{"x": src["width"] // 2 - 40, "y": src["height"] // 2 - 40,
              "w": 80, "h": 80}]
    mapped = map_boxes_to_canvas(boxes, meta["picture_src"])
    assert mapped, "a box at the source centre must survive the mapping"
    y = mapped[0]["y"]
    box = meta["picture_box"]
    assert box["y"] <= y <= box["y"] + box["h"], (
        f"mapped face y={y} is outside the picture strip {box}")
    hidden = map_boxes_to_canvas(
        [{"x": 0, "y": -200, "w": 40, "h": 40}], meta["picture_src"])
    assert not hidden, "a box above the crop must be dropped, not reported"
    print(f"  centre box -> {mapped[0]}, off-frame box dropped")


# --------------------------------------------------------------------------
def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        print(f"\n[{name}]")
        try:
            fn()
        except AssertionError as exc:
            failed.append((name, str(exc)))
            print(f"  FAIL: {exc}")
        except Exception as exc:                              # noqa: BLE001
            failed.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"  ERROR: {type(exc).__name__}: {exc}")
        else:
            print("  ok")
    print("\n" + "=" * 64)
    if failed:
        print(f"FAILED {len(failed)}/{len(tests)}")
        for name, msg in failed:
            print(f"  - {name}: {msg[:160]}")
        return 1
    print(f"PASSED {len(tests)}/{len(tests)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
