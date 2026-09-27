#!/usr/bin/env python3
"""
bgm_ledger.py - BGM 曲库运营台账（2026-09-28 新增）

解决什么问题
------------
用户原话：
  "曲库里的歌曲不应该作为第一选择，应该优先从网上找，然后丰富曲库。
   要不然后续随着视频增多，视频的 BGM 都全相同了，因为曲库不增加啊"

曲库是**消耗品**：8 条视频用掉 8 首，第 9 条就只能复用。不主动扩张，复用必然发生，
听感上就是 §12.4 踩坑里那句"千篇一律"。

**两层模型（2026-09-28 用户二次纠正，务必分清）**

用户原话：
  "逻辑应该是优先网上搜索对应的歌曲，然后在本地曲库找，如果有，就省去下载部署
   直接合成，如果没有就去下载"

这里其实是**两件不同的事**，早先版本把它们压成了一条优先级阶梯，是错的：

    第一层「选哪首歌」= 上网搜（池子是全网的，按视频需求选，不受库存限制）
    第二层「从哪儿供给」= 查本地库（命中就是**缓存**，省掉下载+入册）

所以正确流程是：

    ① 按视频需求（BPM / 风格 / 情绪）**上网搜候选**，排出先后
    ② 逐个候选查本地库：
         - 本地有 **且没用过** → ✅ 命中，**省去下载与入册，直接合成**
         - 本地有 **但用过了**  → ❌ 不算（直接复用会让曲库白扩充），看下一候选
         - 本地没有             → ⬇️ 下载 → `add` 入册 → 合成
    ③ 用完 `record` 记一笔（不记，下次就不知道这首出过场）

**为什么本地命中必须是"没用过"**：本脚本的存在意义就是防复用。若本地有就直接用，
用过的那些会被无限复用 —— 又回到"千篇一律"。

`resolve` 就是把 ② 做成一条命令（给定网搜候选，输出 用本地 / 需下载 的裁决）。
本脚本共 6 个子命令。

⚠️ 一个必须记住的数字：`music/sports/` 有 34 个文件，但**只有 24 首歌** ——
   10 个 `*_variant` / `*_v3` 是同一首的变速指纹变形，观众听来是同一首。
   所以"还剩多少首可换"要按**歌曲**算，不是文件数。本脚本一律按 `normalize_track_key`
   归并（见 common.py），`audit` 给出的也是歌曲数。

子命令
------
  resolve --bpm <X> --candidate <曲名>[::<作者>] [--candidate ...]
                                 **选曲主入口**：给定上网搜到的候选（按优劣排序），逐个裁决
                                 → 本地有且没用过 = 用本地（省下载）/ 本地没有 = 去下载
  audit                          曲库体检：多少首歌 / 用过几首 / 剩几首没用过 / 复用排行
  pick --bpm <X> [--top N]       按视频 BPM 推荐候选，**没用过的优先**
  record --track <file> [--video <path>] [--output-dir <dir>]
                                 记录某条视频用了哪首（写台账 → 下次能被防重复）
  seed --root <dir>              批量导入历史使用记录（扫 <root>/*/work/audio-plan.json）
  add --audio <file> [...]       新曲一键入册：放进 music/sports/ + 实测 BPM + 写登记

存储（都跟随 skill，跨项目统一）
--------------------------------
  configs/bgm-usage.json          使用台账（机器维护，勿手改）
  configs/bgm-library-added.json  自动入册的新曲目（机器维护，勿手改）
  configs/bgm-bpm-measured.json   实测 BPM 库（add 时追加一条）
  configs/bgm-library.yaml        人工维护的曲库主干 —— **本脚本从不写它**（保留注释）

退出码
------
  0 = 成功
  1 = 成功但有警告（如"新鲜曲目不足，该去搜新曲了"）
  2 = 致命错误
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    CONFIGS_DIR, MUSIC_DIR, save_json, load_json, load_yaml, log,
    normalize_track_key, is_variant_track,
)

USAGE_PATH = os.path.join(CONFIGS_DIR, "bgm-usage.json")
ADDED_PATH = os.path.join(CONFIGS_DIR, "bgm-library-added.json")
MEASURED_PATH = os.path.join(CONFIGS_DIR, "bgm-bpm-measured.json")
LIBRARY_YAML = os.path.join(CONFIGS_DIR, "bgm-library.yaml")

# 本地库里"没用过"的歌少于这个数，就提示该去网上找新曲了（≈ 一个批次 8 条）
FRESH_LOW_WATERMARK = 8

# 这些许可不允许用在 TikTok（YouTube Audio Library 标准许可只覆盖 YouTube 站内）
_UNSAFE_LICENSE_KEYWORDS = ("youtube audio library",)

# BPM 偏差超过这个比例就不该再用 atempo 硬拉（再拉观感会变）—— 换曲
BPM_ATEMPO_LIMIT = 0.12

_USAGE_NOTE = (
    "BGM 使用台账：记录每首歌被哪些视频用过，用于『优先用没用过的』与防重复。"
    "键是 normalize_track_key() 归一后的曲名 —— 母版与 *_variant 变速变体归到同一个键，"
    "因为观众听来是同一首歌。机器维护，勿手改；写入用 `bgm_ledger.py record`。"
)
_ADDED_NOTE = (
    "自动入册的新曲目（`bgm_ledger.py add` 写入）。单独放 JSON 而不写进 "
    "bgm-library.yaml，是为了不破坏那份人工曲库里的注释。"
    "generate_post.py 会把两者合并读取（yaml 优先）。"
)


# ============================================================
# 台账读写
# ============================================================
def load_usage():
    data = load_json(USAGE_PATH, None)
    if not isinstance(data, dict) or "tracks" not in data:
        data = {"_note": _USAGE_NOTE, "tracks": {}}
    data.setdefault("tracks", {})
    return data


def save_usage(data):
    data["_note"] = _USAGE_NOTE
    data["_updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_json(USAGE_PATH, data)


def load_measured():
    data = load_json(MEASURED_PATH, {}) or {}
    return data.get("tracks", []) or []


def save_measured(tracks):
    data = load_json(MEASURED_PATH, {}) or {}
    data["tracks"] = tracks
    save_json(MEASURED_PATH, data)


def load_added():
    data = load_json(ADDED_PATH, None)
    if not isinstance(data, dict) or "tracks" not in data:
        data = {"_note": _ADDED_NOTE, "tracks": []}
    return data


def save_added(data):
    data["_note"] = _ADDED_NOTE
    data["_updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_json(ADDED_PATH, data)


# ============================================================
# 曲库视图：按「歌曲」而不是「文件」聚合
# ============================================================
def load_library_meta():
    """从人工维护的 bgm-library.yaml 取 {曲名key: 曲目元数据}（作者/来源/许可）。

    只读不写 —— 那份 yaml 有大量人工注释，程序写回会把注释冲掉（新曲一律进 added.json）。
    """
    if not os.path.exists(LIBRARY_YAML):
        return {}
    try:
        data = load_yaml(LIBRARY_YAML) or {}
    except Exception as exc:  # yaml 坏了不该让选曲直接挂掉
        log.warning(f"Cannot read {LIBRARY_YAML}: {exc}")
        return {}
    meta = {}
    for _cat, body in (data.get("categories") or {}).items():
        for track in (body or {}).get("tracks") or []:
            key = normalize_track_key(track.get("name", ""))
            if key:
                meta[key] = track
    return meta


def library_view():
    """把实测 BPM 库聚合成 {曲名key: {...}}，母版与变体合并为一首歌。

    每首歌保留：代表文件（优先非变体）、实测 BPM、时长、类目、全部文件，
    以及从 bgm-library.yaml / added.json 取到的 作者 / 来源 / 许可。
    """
    songs = {}
    for entry in load_measured():
        f = entry.get("file") or ""
        if not f:
            continue
        key = normalize_track_key(f)
        if not key:
            continue
        slot = songs.setdefault(key, {
            "key": key, "files": [], "paths": [], "bpm": None, "confidence": None,
            "duration_sec": None, "category": entry.get("category"),
            "representative": None, "path": None,
            "artist": None, "source": None, "license": None,
        })
        slot["files"].append(os.path.basename(f))
        slot["paths"].append(f)
        if slot["bpm"] is None or not is_variant_track(f):
            # 首次填值，或遇到非变体（母版）就覆盖为母版的数据
            slot["bpm"] = entry.get("bpm_measured")
            slot["confidence"] = entry.get("confidence")
            slot["duration_sec"] = entry.get("duration_sec")
            if entry.get("category"):
                slot["category"] = entry.get("category")
            if not is_variant_track(f):
                slot["representative"] = os.path.basename(f)
                slot["path"] = f

    # 登记在 added.json 但还没实测的曲目，也纳入视图（bpm 为空）
    for track in load_added().get("tracks", []) or []:
        key = normalize_track_key(track.get("name", ""))
        if key and key not in songs:
            songs[key] = {
                "key": key, "files": [], "paths": [],
                "bpm": track.get("bpm_measured"),
                "confidence": None, "duration_sec": track.get("duration_sec"),
                "category": track.get("category"),
                "representative": track.get("name"), "path": None,
                "artist": track.get("artist"), "source": track.get("source"),
                "license": track.get("license"), "unmeasured": True,
            }

    # 补上作者/来源/许可：added.json（新曲）优先于 yaml（人工主干）
    # ⚠️ 唯独**不**从 yaml 取 `bpm` —— 那份登记值实测 8/8 全错（最差 31%，见 SKILL.md §10.1），
    #    取进来会让错误 BPM 混进 `pick` 的候选排序。没有实测值的曲目就是"不参与节奏匹配"。
    meta = load_library_meta()
    for key, slot in songs.items():
        src = meta.get(key)
        if not src:
            continue
        slot["artist"] = slot["artist"] or src.get("artist")
        slot["source"] = slot["source"] or src.get("source")
        slot["license"] = slot["license"] or src.get("license")

    for slot in songs.values():
        slot["representative"] = slot["representative"] or (slot["files"][0] if slot["files"] else slot["key"])
        slot["path"] = slot["path"] or (slot["paths"][0] if slot["paths"] else None)
        slot["variants"] = [f for f in slot["files"] if is_variant_track(f)]
    return songs


def usage_counts():
    """{曲名key: 使用次数}"""
    return {k: int((v or {}).get("count") or 0)
            for k, v in load_usage()["tracks"].items()}


# ============================================================
# audit
# ============================================================
def cmd_audit(args):
    songs = library_view()
    counts = usage_counts()
    used = {k: c for k, c in counts.items() if c > 0}
    fresh = [k for k in songs if counts.get(k, 0) == 0]
    stale = {k: c for k, c in counts.items() if c > 0 and k not in songs}

    print(f"曲库体检 —— {len(songs)} 首歌（含 {sum(len(s['files']) for s in songs.values())} 个文件，"
          f"其中变体 {sum(len(s['variants']) for s in songs.values())} 个）")
    print(f"  用过     : {len(used)} 首")
    print(f"  没用过   : {len(fresh)} 首")
    if stale:
        print(f"  ⚠️ 台账里有 {len(stale)} 首已不在曲库中（可能被删）：{sorted(stale)}")

    if used:
        print("\n复用排行（次数 > 1 说明观众已经听过重复的 BGM）：")
        for key, n in sorted(used.items(), key=lambda kv: (-kv[1], kv[0])):
            flag = "  ⚠️ 已复用" if n > 1 else ""
            print(f"    {n}x  {songs.get(key, {}).get('representative', key)}{flag}")

    if fresh:
        print(f"\n没用过的歌（{len(fresh)} 首，优先从这里选）：")
        for key in sorted(fresh, key=lambda k: -(songs[k].get("bpm") or 0)):
            s = songs[key]
            bpm = f"{s['bpm']:.1f}" if s.get("bpm") else "?"
            var = " (+变体)" if s.get("variants") else ""
            print(f"    BPM {bpm:>6s}  {s['representative']}{var}")

    warnings = []
    if len(fresh) < FRESH_LOW_WATERMARK:
        warnings.append(
            f"没用过的歌只剩 {len(fresh)} 首（低于 {FRESH_LOW_WATERMARK}）—— "
            f"下一批按 §12.4「两层模型」：**先上网搜**，再用 `resolve` 逐个查本地库；"
            f"裁决为「需下载」时用 `bgm_ledger.py add` 入册，否则必须复用。")
        print(f"\n🔴 {' '.join(warnings)}")
    else:
        print(f"\n✅ 没用过的歌 {len(fresh)} 首，够撑约 {len(fresh)} 条不重复的视频。")

    result = {
        "songs_total": len(songs), "files_total": sum(len(s["files"]) for s in songs.values()),
        "variants_total": sum(len(s["variants"]) for s in songs.values()),
        "songs_used": len(used), "songs_fresh": len(fresh),
        "reuse": {k: c for k, c in counts.items() if c > 1},
        "fresh_list": sorted(fresh),
        "warnings": warnings,
    }
    if args.json:
        print("\n" + json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if warnings else 0, result


# ============================================================
# pick
# ============================================================
def cmd_pick(args):
    songs = library_view()
    counts = usage_counts()
    target = float(args.bpm)

    rows = []
    for key, s in songs.items():
        bpm = s.get("bpm")
        if not bpm:
            continue
        delta = abs(bpm - target) / target
        if delta > args.tolerance:
            continue
        uses = counts.get(key, 0)
        rows.append({
            "key": key, "track": s["representative"], "bpm": bpm, "delta": delta,
            "uses": uses, "variants": s["variants"],
            "license": (s.get("license") or ""),
        })

    # 排序：**没用过的优先**（这是用户的硬要求），其次 BPM 更接近，再次用得少的
    rows.sort(key=lambda r: (r["uses"] > 0, r["delta"], r["uses"]))

    if not rows:
        print(f"❌ 没有任何曲目的实测 BPM 落在 {target}±{args.tolerance * 100:.0f}% 内。")
        print("   → 按 §12.4「两层模型」：**先上网搜**，再用 "
              "`bgm_ledger.py resolve --bpm <X> --candidate \"<曲名>\"` 查本地，")
        print("     裁决「需下载」时用 `bgm_ledger.py add --audio <文件>` 入册。")
        return 1, {"candidates": [], "target_bpm": target}

    print(f"视频 BPM {target:.1f} 的本地候选（未用过的排前面）：\n")
    print(f"  {'#':>2s}  {'BPM':>6s}  {'偏差':>6s}  {'用过':>4s}  曲目")
    for i, r in enumerate(rows[:args.top], 1):
        tag = "✅ 新鲜" if r["uses"] == 0 else f"{r['uses']}x 已用"
        var = " (+变体)" if r["variants"] else ""
        print(f"  {i:>2d}  {r['bpm']:>6.1f}  {r['delta'] * 100:>5.1f}%  {tag:>8s}  {r['track']}{var}")

    best = rows[0]
    if best["uses"] > 0:
        print(f"\n⚠️ 所有落在容差内的曲目都用过了（最少的也用了 {best['uses']} 次）。")
        print("   → 按 §12.4「两层模型」：**先上网搜**再 `resolve` 查本地，别直接复用。")
        return 1, {"candidates": rows[:args.top], "target_bpm": target}
    print(f"\n→ 本地首选：{best['track']}（BPM {best['bpm']:.1f}，没用过）")
    print("   ⚠️ 这只是**本地库存**的答案，不等于最好的歌 —— 主路径是上网搜 + "
          "`resolve`（§12.4）")
    return 0, {"candidates": rows[:args.top], "target_bpm": target, "best": best["track"]}


# ============================================================
# resolve（选曲主入口：网搜候选 → 查本地 → 用本地 / 去下载）
# ============================================================
V_USE_LOCAL = "USE_LOCAL"
V_DOWNLOAD = "DOWNLOAD"
V_REJECT_USED = "REJECT_USED"
V_REJECT_UNSAFE = "REJECT_UNSAFE"
V_REJECT_DUP = "REJECT_DUPLICATE"

_VERDICT_LABEL = {
    V_USE_LOCAL: "✅ 用本地",
    V_DOWNLOAD: "⬇️ 需下载",
    V_REJECT_USED: "❌ 已用过",
    V_REJECT_UNSAFE: "🚫 许可不行",
    V_REJECT_DUP: "❌ 重复候选",
}


def parse_candidate(raw):
    """`Name::Artist` → (name, artist)。作者可省。"""
    name, _, artist = str(raw or "").partition("::")
    return name.strip(), (artist.strip() or None)


def _fresh_local(songs, counts, target, tolerance=0.15):
    """本地库中「没用过」的曲目，按与 target 的 BPM 接近度排序。"""
    out = []
    for key, s in songs.items():
        if counts.get(key, 0) > 0:
            continue
        bpm = s.get("bpm")
        delta = abs(bpm - target) / target if (bpm and target) else None
        out.append({"key": key, "track": s["representative"], "path": s.get("path"),
                    "bpm": bpm, "delta": delta,
                    "within": delta is not None and delta <= tolerance})
    out.sort(key=lambda r: (not r["within"], r["delta"] if r["delta"] is not None else 9.9))
    return out


def cmd_resolve(args):
    songs = library_view()
    counts = usage_counts()
    known = load_library_meta()
    target = float(args.bpm) if args.bpm else None
    raw_cands = list(args.candidate or [])

    if target:
        print(f"视频目标 BPM: {target:.1f}（来自 rhythm.suggested_bpm）")

    # ---- 没给候选 = 还没上网搜 ----
    if not raw_cands:
        print("\n❌ 还没有候选。按 §12.4「两层模型」的**第一层**：先按视频需求上网搜，")
        print("   再把搜到的曲名按优劣顺序传进来（可重复）：")
        print('     python scripts/bgm_ledger.py resolve --bpm %.1f \\' % (target or 0))
        print('         --candidate "曲名1::作者1" --candidate "曲名2" ...')
        print("\n（本地库存仅供参考：网搜命中的歌若在这张表里，就能省掉下载）")
        for r in _fresh_local(songs, counts, target)[:10]:
            bpm = f"{r['bpm']:.1f}" if r["bpm"] else "  ?  "
            print(f"    BPM {bpm:>6s}  {r['track']}")
        return 1, {"decision": None, "reason": "no_candidates", "target_bpm": target}

    # ---- 逐个裁决 ----
    rows, seen = [], set()
    for i, raw in enumerate(raw_cands, 1):
        name, artist = parse_candidate(raw)
        key = normalize_track_key(name)
        row = {"order": i, "candidate": name, "artist": artist, "key": key,
               "verdict": None, "reason": "", "uses": None, "track": None,
               "path": None, "bpm": None, "delta": None, "license": None,
               "registered": False}
        if not key:
            row.update(verdict=V_REJECT_DUP, reason="曲名为空")
        elif key in seen:
            row.update(verdict=V_REJECT_DUP, reason="与前面某个候选是同一首")
        else:
            seen.add(key)
            s = songs.get(key)
            if s is None:
                # 只在 bgm-library.yaml 登记过、但本地**没有音频文件**的曲目：
                # 不能直接合成（"本地有"指的是音频真在 music/ 里），但元数据与许可已知，
                # 所以许可不行的要在这里就拦下，别等 add 完才发现。
                k = known.get(key) or {}
                lic = (k.get("license") or "")
                row["registered"] = bool(k)
                row["license"] = lic or None
                if any(x in lic.lower() for x in _UNSAFE_LICENSE_KEYWORDS):
                    row.update(verdict=V_REJECT_UNSAFE,
                               reason=f"本地无音频文件；且许可『{lic}』只覆盖 YouTube 站内，不能用于 TikTok")
                else:
                    row.update(verdict=V_DOWNLOAD,
                               reason="本地没有音频文件"
                                      + ("（曲库已登记元数据，add 时免重复填）" if k else ""))
            else:
                lic = (s.get("license") or "")
                n = counts.get(key, 0)
                bpm = s.get("bpm")
                row.update(track=s["representative"], path=s.get("path"),
                           uses=n, license=lic or None, bpm=bpm,
                           delta=(abs(bpm - target) / target if (bpm and target) else None))
                if any(k in lic.lower() for k in _UNSAFE_LICENSE_KEYWORDS):
                    row.update(verdict=V_REJECT_UNSAFE,
                               reason=f"许可『{lic}』只覆盖 YouTube 站内，不能用于 TikTok")
                elif n > 0:
                    row.update(verdict=V_REJECT_USED,
                               reason=f"本地有，但已经用过 {n} 次 —— 直接复用等于没扩充曲库")
                else:
                    row.update(verdict=V_USE_LOCAL, reason="本地已有且没用过 → 省去下载与入册")
                # 同名不同作者：本地是按**曲名**归一的，撞名会被误判成命中，必须提示
                if artist and s.get("artist") and artist.lower() != str(s["artist"]).lower():
                    row["reason"] += f"；⚠️ 本地同名曲作者是『{s['artist']}』，请确认是否同一首"
        rows.append(row)

    print(f"\n候选裁决（按你给的顺序，共 {len(rows)} 个）：\n")
    print(f"  {'#':>2s}  {'裁决':<10s} {'BPM':>6s}  {'本地库存':<28s} 说明")
    for r in rows:
        bpm = f"{r['bpm']:.1f}" if r["bpm"] else "  -  "
        stock = r["track"] or ("(仅登记，无音频)" if r.get("registered") else "(无)")
        if r["uses"] is not None:
            stock += " 已用 %dx" % r["uses"] if r["uses"] else " 新鲜"
        print(f"  {r['order']:>2d}  {_VERDICT_LABEL[r['verdict']]:<10s} {bpm:>6s}  {stock:<28s} {r['reason']}")

    winner = next((r for r in rows if r["verdict"] in (V_USE_LOCAL, V_DOWNLOAD)), None)
    if winner is None:
        print("\n❌ 所有候选都不可用（已用过 / 许可不行 / 重复）。")
        print("   → 继续上网搜，换一批候选再来；或看下面本地还没用过的：")
        for x in _fresh_local(songs, counts, target)[:6]:
            bpm = f"{x['bpm']:.1f}" if x["bpm"] else "  ?  "
            print(f"     BPM {bpm:>6s}  {x['track']}")
        return 1, {"decision": None, "reason": "all_rejected", "rows": rows,
                   "target_bpm": target}

    print()
    if winner["verdict"] == V_USE_LOCAL:
        lp = winner["path"] or f"music/sports/{winner['track']}"
        print(f"✅ 裁决：用本地库存 —— **不需要下载**（{winner['candidate']}）")
        print(f"   曲目   : {winner['track']}"
              + (f"  BPM {winner['bpm']:.1f}" if winner["bpm"] else ""))
        print(f"   资产   : {lp}")
        if winner["delta"] is not None and winner["delta"] > BPM_ATEMPO_LIMIT:
            print(f"   ⚠️ 节奏偏差 {winner['delta'] * 100:.1f}%（BPM {winner['bpm']:.1f} "
                  f"vs 视频 {target:.1f}）—— 超过 atempo 能自然补偿的范围"
                  f"（±{BPM_ATEMPO_LIMIT * 100:.0f}%，再拉观感会变）。"
                  f"要么换一个 BPM 更近的候选，要么接受节奏差异")
        print("   下一步 :")
        print(f"     python scripts/analyze_bgm.py --bgm {lp} --work-dir work "
              f"--track-name \"{winner['candidate']}\"")
        print(f"     # 混音完成后记账：")
        print(f"     python scripts/bgm_ledger.py record --track {lp} "
              f"--video <源视频> --output-dir <产出目录>")
    else:
        print(f"⬇️ 裁决：本地没有 → **去下载**（{winner['candidate']}）")
        if winner.get("registered"):
            print(f"   ℹ️ 这首歌在曲库里已有登记元数据（只是没音频文件），"
                  f"add 时作者/来源/许可可省")
        print(f"   下载后 :")
        print(f"     python scripts/bgm_ledger.py add --audio <下载的文件> "
              f"--name \"{winner['candidate']}\""
              + (f" --artist \"{winner['artist']}\"" if winner["artist"] else " --artist <作者>")
              + " --source <来源> --license \"CC BY 3.0\" --category <类目>")
        print(f"     # 常见 CC 源（Kevin MacLeod，CC BY 3.0）：")
        print(f"     # https://incompetech.com/music/royalty-free/mp3-royaltyfree/"
              f"<曲名>.mp3")
        if target:
            print(f"   ⚠️ add 会自动实测 BPM；若与视频 BPM {target:.1f} 差 >"
                  f"{BPM_ATEMPO_LIMIT * 100:.0f}%（atempo 的自然补偿上限）"
                  f"就换下一个候选，别硬拉")
        else:
            print(f"   ⚠️ add 会自动实测 BPM；与视频节奏差太远（>"
                  f"{BPM_ATEMPO_LIMIT * 100:.0f}%）就换下一个候选，别硬拉")
        alt = next((r for r in rows if r["verdict"] == V_USE_LOCAL), None)
        if alt:
            print(f"   💡 省下载备选：第 {alt['order']} 候选《{alt['candidate']}》本地已有且没用过"
                  f"（{alt['track']}）—— 若愿意略微放宽拟合度，可直接用它、免下载")

    result = {"decision": winner["verdict"], "picked": winner["candidate"],
              "track": winner["track"], "path": winner["path"],
              "rows": rows, "target_bpm": target}
    if getattr(args, "json", False):
        print("\n" + json.dumps(result, ensure_ascii=False, indent=2))
    return 0, result


# ============================================================
# record / seed
# ============================================================
def _record_one(track, video=None, output_dir=None, profile=None, used_at=None):
    key = normalize_track_key(track)
    if not key:
        return None
    usage = load_usage()
    slot = usage["tracks"].setdefault(key, {"display": os.path.basename(str(track)), "uses": []})
    for u in slot["uses"]:
        if u.get("video") == video and u.get("output_dir") == output_dir:
            return key  # 同一条视频重复记录，忽略
    slot["uses"].append({
        "video": video, "output_dir": output_dir, "profile": profile,
        "used_at": used_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    slot["count"] = len(slot["uses"])
    slot["first_used_at"] = slot["uses"][0]["used_at"]
    slot["last_used_at"] = slot["uses"][-1]["used_at"]
    save_usage(usage)
    return key


def cmd_record(args):
    if not args.track:
        print("❌ --track 必填"); return 2, {}
    key = _record_one(args.track, args.video, args.output_dir, args.profile, args.used_at)
    if not key:
        print("❌ 曲名为空"); return 2, {}
    n = usage_counts().get(key, 0)
    flag = "  ⚠️ 已经用过 %d 次了" % n if n > 1 else ""
    print(f"已记录：{os.path.basename(args.track)} → {key}（累计 {n} 次）{flag}")
    return 1 if n > 1 else 0, {"track_key": key, "count": n}


def cmd_seed(args):
    """扫 <root>/*/work/audio-plan.json，把历史使用记录导入台账。"""
    import glob
    pattern = os.path.join(args.root, "*", "work", "audio-plan.json")
    added, skipped = 0, 0
    for ap in sorted(glob.glob(pattern)):
        plan = load_json(ap, {}) or {}
        asset = ((plan.get("bgm") or {}).get("asset_path"))
        if not asset:
            skipped += 1
            continue
        out_dir = os.path.dirname(os.path.dirname(ap))
        video = os.path.basename(out_dir)
        if _record_one(asset, video, out_dir, args.profile):
            added += 1
            print(f"  + {os.path.basename(str(asset)):<28s} ← {video[:40]}")
        else:
            skipped += 1
    print(f"\n导入 {added} 条，跳过 {skipped} 条（无 BGM 或已存在）。")
    return 0, {"imported": added, "skipped": skipped}


# ============================================================
# add（新曲入册：① 上网搜新曲 的落地动作）
# ============================================================
def cmd_add(args):
    src = args.audio
    if not os.path.isfile(src):
        print(f"❌ 音频文件不存在：{src}"); return 2, {}

    name = args.name or os.path.splitext(os.path.basename(src))[0]
    key = normalize_track_key(name)
    songs = library_view()
    if key in songs and not args.force:
        print(f"❌ 曲库里已经有同一首歌（key={key}，代表文件 "
              f"{songs[key]['representative']}）。加进来只会让『不同歌曲数』不变。")
        print("   确认是无损替换/补充变体再加 --force。")
        return 2, {}

    # 1) 放进 music/sports/
    dest_dir = os.path.join(MUSIC_DIR, args.subdir)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))
    if os.path.abspath(src) != os.path.abspath(dest):
        shutil.copy2(src, dest)
        print(f"[1/3] 已放入曲库：{os.path.relpath(dest, os.path.dirname(MUSIC_DIR))}")

    # 2) 实测 BPM（选曲一律按实测值，不能用登记值）
    bpm, conf, dur = args.bpm, None, None
    if args.bpm:
        print(f"[2/3] BPM 用你给的值 {args.bpm}（跳过实测）")
    elif args.no_measure:
        print("[2/3] 跳过 BPM 实测（--no-measure）—— 该曲暂时无法参与节奏匹配")
    else:
        try:
            import analyze_bgm  # 同目录，复用已自测 5/5 的 BPM 估计
            rel = os.path.relpath(dest, os.path.dirname(MUSIC_DIR))
            res = analyze_bgm.analyze(dest, rhythm=None, track_name=os.path.basename(dest))
            bpm = res.get("bpm_measured")
            conf = res.get("bpm_confidence")
            dur = res.get("bgm_duration_sec")
            print(f"[2/3] 实测 BPM {bpm}（confidence {conf}，时长 {dur}s）")
            if bpm is None:
                print("      ⚠️ BPM 估计失败（音频过短或无 onset），该曲不参与节奏匹配")
        except Exception as exc:
            print(f"[2/3] ⚠️ BPM 实测失败（{exc}）—— 已入册，但暂不参与节奏匹配")

    # 3) 写登记（added.json + 实测库）
    added = load_added()
    license_str = args.license or ""
    entry = {
        "name": name, "artist": args.artist, "source": args.source,
        "license": license_str, "category": args.category, "style": args.style,
        "mood": args.mood, "energy": args.energy, "source_url": args.url,
        "bpm_measured": bpm, "duration_sec": dur,
        "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    added["tracks"] = [t for t in added.get("tracks", []) if normalize_track_key(t.get("name", "")) != key]
    added["tracks"].append(entry)
    save_added(added)

    if bpm is not None or dur is not None:
        rel = os.path.relpath(dest, os.path.dirname(MUSIC_DIR))
        tracks = [t for t in load_measured()
                  if os.path.basename(t.get("file") or "") != os.path.basename(dest)]
        tracks.append({
            "file": rel, "bpm_measured": bpm, "bpm_declared": None,
            "confidence": conf, "duration_sec": dur,
            "category": args.category, "is_variant": is_variant_track(dest),
        })
        save_measured(tracks)
    print(f"[3/3] 已入册：曲库现 {len(library_view())} 首歌")

    if any(k in license_str.lower() for k in _UNSAFE_LICENSE_KEYWORDS):
        print(f"\n🚫 警告：许可『{license_str}』只覆盖 YouTube 站内，**不能用于 TikTok** —— "
              f"这首别用（署名也救不了）。")
        return 1, {"track_key": key, "unsafe_license": license_str}

    if not license_str:
        print("\n⚠️ 没写 --license —— 建议补上（CC BY 3.0 等），否则署名与合规无法自动判断。")
    return 0, {"track_key": key, "bpm_measured": bpm, "name": name}


# ============================================================
# CLI
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="BGM 曲库运营台账（防重复 / 促增长）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("resolve", help="选曲主入口：网搜候选 → 查本地 → 用本地/去下载")
    p.add_argument("--bpm", type=float, help="视频 suggested_bpm（来自 rhythm.json）")
    p.add_argument("--candidate", action="append", default=[],
                   help="网搜到的候选曲名，按优劣顺序传（可重复）；要带作者写 '曲名::作者'")
    p.add_argument("--json", action="store_true", help="额外打印 JSON 结论")
    p.set_defaults(fn=cmd_resolve)

    p = sub.add_parser("audit", help="曲库体检")
    p.add_argument("--json", action="store_true", help="额外打印 JSON")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("pick", help="兜底：只按本地库存推荐（主路径请用 resolve）")
    p.add_argument("--bpm", required=True, type=float, help="视频 suggested_bpm")
    p.add_argument("--tolerance", type=float, default=0.15, help="BPM 容差比例（默认 0.15）")
    p.add_argument("--top", type=int, default=8, help="展示条数（默认 8）")
    p.set_defaults(fn=cmd_pick)

    p = sub.add_parser("record", help="记录某条视频用了哪首")
    p.add_argument("--track", required=True, help="曲目文件路径或曲名")
    p.add_argument("--video", help="源视频路径/名称")
    p.add_argument("--output-dir", help="产出目录")
    p.add_argument("--profile", help="使用的 profile")
    p.add_argument("--used-at", help="使用时间（ISO），默认当前 UTC")
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("seed", help="批量导入历史使用记录")
    p.add_argument("--root", required=True, help="含 */work/audio-plan.json 的根目录")
    p.add_argument("--profile", help="统一标记的 profile")
    p.set_defaults(fn=cmd_seed)

    p = sub.add_parser("add", help="新曲入册（曲库 +1）")
    p.add_argument("--audio", required=True, help="下载好的音频文件")
    p.add_argument("--name", help="曲名（默认取文件名）")
    p.add_argument("--artist", help="作者")
    p.add_argument("--source", help="来源平台，如 Incompetech")
    p.add_argument("--license", help="许可，如 'CC BY 3.0'")
    p.add_argument("--category", help="类目，如 high_energy")
    p.add_argument("--style", help="风格，如 rock/hiphop")
    p.add_argument("--mood", help="情绪，如 intense/epic")
    p.add_argument("--energy", type=int, help="能量 1-10")
    p.add_argument("--url", help="来源页 URL")
    p.add_argument("--subdir", default="sports", help="music/ 下的子目录（默认 sports）")
    p.add_argument("--bpm", type=float, help="已知 BPM（给了就跳过实测）")
    p.add_argument("--no-measure", action="store_true", help="跳过 BPM 实测")
    p.add_argument("--force", action="store_true", help="允许重复登记同一首歌")
    p.set_defaults(fn=cmd_add)

    args = ap.parse_args()
    code, _ = args.fn(args)
    sys.exit(code)


if __name__ == "__main__":
    main()
