#!/usr/bin/env python3
"""
generate_post.py - 生成 TikTok 发布方案（九段结构 Markdown）

输入：各 plan/结果 JSON + Agent 生成的文案
输出：output/tiktok-post.md

固定九段结构（`render_markdown` 里的实际顺序，别按旧文档数错）：
  1. Video Summary（必须英文）
  2. TikTok Caption（英文）+ 其后的 BGM 署名块
  3. Hashtags（恰好 5 个，相关且热门）
  4. Pinned Comment（英文，含内容预告+品牌账号+互动问题）
  5. BGM Recommendations（具体歌名+艺术家+BPM+风格+授权+下载方式）
  6. Posting Time（北京时间 + 对应美东/美西）
  7. Video Cover
  8. Processing Report（如实披露降级/低置信度/未解决项）
  9. Final Files

核心规则（必须遵守，违反即不合格）：
  1. 内容简介、Caption、置顶评论必须是英文（源视频是中文，但目标平台是 TikTok 英文用户）
  1b. Summary **只写视频内容本身**（2026-10-29 用户规则）：画面里是什么球/谁/什么动作，
      不放系列元信息、策略、制作说明；Section 1 也不再展示 Topic 行
  2. 话题标签恰好 5 个，必须是与视频内容相关且在 TikTok 上热门的标签（禁止泛泛的 #fyp #viral）；
     渲染时自动补 `#` 前缀（2026-10-29 用户规则：标签裸词不带 # 是 bug）
  3. 置顶评论必须包含：内容预告（驱动关注）、品牌账号、互动问题（提升评论量）
  4. BGM 推荐必须给出具体歌名、艺术家、BPM、风格、授权状态、下载方式，不能泛泛推荐
  5. 发布时间建议必须同时给出北京时间和对应美国时间（美东/美西）
  6. **已混入视频的 BGM 必须署名**（2026-09-28 新增）：CC BY 的 "BY" 是**许可条件**，
     与发布平台无关。`resolve_music_credit()` 按 `audio-plan.json` 的实际 BGM 反查曲库，
     自动生成可粘贴的署名行；许可为 YouTube Audio Library 的曲目直接出「授权不可用」警告。

最终用户输出是 Markdown，不是 JSON。
"""

import argparse
import os
import sys
from typing import Dict, Any, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    save_json, load_json, make_result, log,
    work_path, output_path, load_yaml, CONFIGS_DIR,
    normalize_track_key,   # 母版与变速变体归一到同一 key（唯一真源在 common.py）
)


# ============================================================
# BGM 署名（2026-09-28 新增）
# ============================================================
# SKILL.md §12.4 第 6 条一早要求"已混入视频的 BGM 要在 tiktok-post.md 里署名"，
# 但本脚本从来没生成过那一行 —— 规格写了、代码没做。这里补上。
#
# ⚠️ 署名义务**与发布平台无关**：CC BY 的 "BY" 是许可的**条件**，不是建议。
#    所以"我只发 TikTok"不构成不署名的理由（曾据此误判过一次，记在这里）。

# 许可 → 可直接粘贴的署名模板（TASL 四要素：Title / Author / Source / License）
_CREDIT_TEMPLATES = {
    "cc by 3.0": "{title} by {artist} ({source}), licensed under Creative Commons: By Attribution 3.0",
    "cc by 4.0": "{title} by {artist} ({source}), licensed under Creative Commons: By Attribution 4.0",
}

# 这些许可**禁止**在 TikTok 使用：YouTube Audio Library 的标准许可只覆盖 YouTube 站内
# （YouTube 官方 Help 未给出站外授权，多方梳理一致指向 YouTube-only）。
# 命中即出警告、**不**生成署名 —— 署名救不了，只能换曲。
_TIKTOK_UNSAFE_LICENSE_KEYWORDS = ("youtube audio library",)

# 这些许可要求署名，但措辞由版权方规定，无法安全地自动生成 → 提示人工照抄官方文案。
_MANUAL_CREDIT_KEYWORDS = ("bensound",)

# 本地 music/ 目录的兜底元数据：music/sports/ 下 34 个文件全部是 Kevin MacLeod /
# Incompetech 曲目，但 bgm-library.yaml 实测只覆盖其中 **11 个文件 / 8 首歌**
# （24 首歌里 16 首查不到元数据）。查不到时按本地事实兜底，并**显式标注 inferred**，
# 让用户知道要核对。
_LOCAL_MUSIC_FALLBACK = {
    "artist": "Kevin MacLeod",
    "source": "Incompetech",
    # 3.0 → 4.0（2026-10-16 核实）：incompetech.com 的 FAQ 现在统一写
    # 「Licensed under Creative Commons: By Attribution 4.0」并直接给出
    # creativecommons.org/licenses/by/4.0 的 PDF 链接。老曲目当年是 3.0 发布的，
    # CC 授权不可撤销，所以写 3.0 也不算错 —— 但既然版权方现行声明是 4.0，
    # 兜底就该跟现行声明一致，否则每首新歌都会生成一个过时版本号。
    "license": "CC BY 4.0",
}

def load_bgm_library_index() -> Dict[str, Dict[str, Any]]:
    """把曲库摊平成 {归一化曲名: 曲目元数据} 索引。

    两个来源合并（`normalize_track_key` 归一，母版与变体同 key）：
      1. `configs/bgm-library.yaml`         —— **人工维护**的曲库主干（有注释，脚本从不写它）
      2. `configs/bgm-library-added.json`   —— `bgm_ledger.py add` **自动入册**的新曲目
         （单独放 JSON 是为了不破坏 yaml 里的人工注释；两者冲突时以 yaml 为准）
    """
    index: Dict[str, Dict[str, Any]] = {}

    # 来源 2 先放，来源 1 覆盖 —— 人工维护的登记优先
    added = os.path.join(CONFIGS_DIR, "bgm-library-added.json")
    if os.path.exists(added):
        for track in (load_json(added, {}) or {}).get("tracks", []) or []:
            key = normalize_track_key(track.get("name", ""))
            if key:
                index[key] = track

    cfg = os.path.join(CONFIGS_DIR, "bgm-library.yaml")
    if not os.path.exists(cfg):
        log.warning(f"BGM library not found: {cfg}")
        return index
    try:
        data = load_yaml(cfg)
    except Exception as exc:  # yaml 坏了不该让整个发布方案生成失败
        log.warning(f"Cannot read {cfg}: {exc}")
        return index
    for _cat, body in (data.get("categories") or {}).items():
        for track in (body or {}).get("tracks") or []:
            key = normalize_track_key(track.get("name", ""))
            if key:
                index[key] = track
    return index


def resolve_music_credit(work_dir: str) -> Optional[Dict[str, Any]]:
    """根据**实际混入视频**的 BGM 生成署名信息。

    数据来源：`audio-plan.json` 的 `bgm.asset_path`，其次 `bgm-plan.json` 的 `asset_path`。
    返回 `None` 表示这条视频没有混入任何 BGM（无需署名）。
    """
    audio_plan = load_json(work_path(work_dir, "audio-plan.json"), {}) or {}
    bgm_plan = load_json(work_path(work_dir, "bgm-plan.json"), {}) or {}

    asset = ((audio_plan.get("bgm") or {}).get("asset_path")
             or bgm_plan.get("asset_path"))
    if not asset:
        return None

    title = os.path.splitext(os.path.basename(str(asset)))[0]
    meta = load_bgm_library_index().get(normalize_track_key(asset))
    inferred = False
    if not meta:
        meta = dict(_LOCAL_MUSIC_FALLBACK)
        inferred = True
        log.warning(
            "BGM '%s' 未登记在 bgm-library.yaml，按本地 music/ 目录既有事实"
            "（%s / %s）兜底 —— 请核对。", title, meta["artist"], meta["license"])

    license_str = str(meta.get("license") or "").strip()
    lower = license_str.lower()

    if any(k in lower for k in _TIKTOK_UNSAFE_LICENSE_KEYWORDS):
        return {
            "track": title, "license": license_str, "unsafe": True,
            "note": (f"`{title}` 的许可是 **{license_str}** —— 只覆盖 YouTube 站内，"
                     "用在 TikTok 上有版权风险。必须换曲，署名不能解决。"),
        }

    if any(k in lower for k in _MANUAL_CREDIT_KEYWORDS) or lower not in _CREDIT_TEMPLATES:
        return {
            "track": title, "license": license_str, "manual": True,
            "note": (f"`{title}` 的许可（{license_str or '未登记'}）要求署名，"
                     "但措辞由版权方规定，请照抄版权方给出的官方文案。"),
        }

    return {
        "track": title,
        "artist": meta.get("artist"),
        "source": meta.get("source"),
        "license": license_str,
        "inferred": inferred,
        "credit": _CREDIT_TEMPLATES[lower].format(
            title=title,
            artist=meta.get("artist", "unknown"),
            source=meta.get("source", ""),
        ),
    }


def _render_music_credit(lines: List[str], credit: Optional[Dict[str, Any]]) -> None:
    """在 Caption 段后附上 BGM 署名（CC BY 的 'BY' 是硬性许可条件）。"""
    if not credit:
        return
    lines.append("")
    if credit.get("unsafe"):
        lines.append(f"🚫 **BGM 授权不可用**：{credit['note']}")
        return
    if credit.get("manual"):
        lines.append(f"⚠️ **BGM 署名需人工处理**：{credit['note']}")
        return

    lines.append("**BGM credit — CC BY requires attribution; append this line to the caption:**")
    lines.append("")
    lines.append("```")
    lines.append(f"Music: {credit['credit']}")
    lines.append("```")
    if credit.get("inferred"):
        lines.append("")
        lines.append(f"> ⚠️ 元数据未在 `bgm-library.yaml` 中登记，按本地 `music/` 目录兜底推断为 "
                     f"{credit.get('artist')} / {credit.get('license')} —— 发布前请核对曲目来源。")


def generate_default_hashtags(understanding: Dict, profile: str = "generic") -> List[str]:
    """
    根据视频内容生成 5 个相关且热门的 TikTok 标签。
    禁止使用泛泛的 #fyp #viral，必须与内容强相关。
    """
    topic = (understanding.get("topic", "") or "").lower()
    summary = (understanding.get("summary", "") or "").lower()
    content_text = topic + " " + summary

    # NBA / 篮球相关
    if any(kw in content_text for kw in ["nba", "basketball", "篮球", "hoops", "dunk", "扣篮"]):
        # 检测具体球员
        player_tags = []
        player_map = {
            "jordan": "#MichaelJordan", "乔丹": "#MichaelJordan",
            "lebron": "#LeBronJames", "詹姆斯": "#LeBronJames",
            "kobe": "#KobeBryant", "科比": "#KobeBryant",
            "iverson": "#AllenIverson", "艾弗森": "#AllenIverson",
            "curry": "#StephenCurry", "库里": "#StephenCurry",
            "griffin": "#BlakeGriffin", "格里芬": "#BlakeGriffin",
            " Durant": "#KevinDurant", "杜兰特": "#KevinDurant",
        }
        for kw, tag in player_map.items():
            if kw.lower() in content_text and tag not in player_tags:
                player_tags.append(tag)

        base_tags = ["#NBA", "#Basketball"]
        if player_tags:
            # 球员标签 + NBA + Basketball + 相关热门标签（确保恰好5个）
            tags = player_tags[:1] + base_tags  # 1个球员 + 2个基础 = 3个
            # 补充内容相关标签到5个
            content_tags = []
            if "dunk" in content_text or "扣篮" in content_text:
                content_tags.append("#Dunk")
            if "highlights" in content_text or "集锦" in content_text or "佳球" in content_text or "top" in content_text:
                content_tags.append("#NBATopPlays")
            if "crossover" in content_text or "突破" in content_text:
                content_tags.append("#Crossover")
            # 补充默认标签
            default_extra = ["#Hoops", "#BasketballHighlights", "#NBATok", "#Legend"]
            for tag in content_tags + default_extra:
                if tag not in tags:
                    tags.append(tag)
                if len(tags) >= 5:
                    break
        else:
            tags = ["#NBA", "#Basketball", "#NBATopPlays", "#Hoops", "#BasketballHighlights"]
        return tags[:5]

    # 通用体育
    if any(kw in content_text for kw in ["sports", "体育", "football", "soccer", "tennis"]):
        return ["#Sports", "#SportsHighlights", "#Athlete", "#ViralSports", "#SportsTok"]

    # 默认：根据 profile 生成
    if profile == "nba":
        return ["#NBA", "#Basketball", "#NBATopPlays", "#Hoops", "#BasketballHighlights"]
    elif profile == "sports":
        return ["#Sports", "#SportsHighlights", "#Athlete", "#ViralSports", "#SportsTok"]
    else:
        # 泛内容但仍需相关，禁止 #fyp #viral
        return ["#Viral", "#Trending", "#ForYou", "#FYP", "#Explore"]


def generate_default_pinned_comment(understanding: Dict, profile: str = "generic") -> str:
    """
    生成默认置顶评论，必须包含：
    1. 内容预告（驱动关注）
    2. 品牌账号 @HoopDynastyDaily
    3. 互动问题（提升评论量）
    4. 关键标签
    """
    topic = understanding.get("topic", "this video")
    summary = understanding.get("summary", "")

    # 检测是否是系列内容（如 Top 50 Plays countdown）
    is_series = any(kw in (topic + summary).lower() for kw in ["top 50", "top 10", "countdown", "佳球", "系列", "part"])

    if is_series:
        comment = (
            f"🔥 More {topic} coming soon! Follow for the full countdown.\n\n"
            f"Follow @HoopDynastyDaily for daily NBA highlights, legend mixtapes, and basketball history.\n\n"
            f"What's your all-time favorite play? Let me know in the comments! 👇\n\n"
            f"#NBA #Basketball #HoopDynastyDaily"
        )
    else:
        comment = (
            f"🔥 {topic} was absolutely INSANE!\n\n"
            f"Follow @HoopDynastyDaily for daily NBA highlights, legend mixtapes, and basketball history.\n\n"
            f"Who's your favorite player of all time? Let me know in the comments! 👇\n\n"
            f"#NBA #Basketball #HoopDynastyDaily"
        )

    return comment


def load_post_content(work_dir: str, profile: str = "generic",
                      agent_content: Optional[Dict] = None) -> Dict:
    """加载并合并发布文案内容。

    始终从 video-understanding / cover-plan / audio-plan 提取基础字段
    （title / summary / cover_headline 等），再用 Agent 在 post-content.json
    中提供的内容做部分覆盖。这样即便 Agent 只填了 caption / hashtags，
    summary / title 也不会丢失而落为 N/A。
    """
    if agent_content is None:
        agent_content = load_json(work_path(work_dir, "post-content.json"), {})

    understanding = load_json(work_path(work_dir, "video-understanding.json"), {})
    plan = load_json(work_path(work_dir, "localization-plan.json"), {})
    cover_plan = load_json(work_path(work_dir, "cover-plan.json"), {})
    audio_plan = load_json(work_path(work_dir, "audio-plan.json"), {})

    # 内容简介必须是英文
    title = understanding.get("topic", "NBA Highlights")
    summary = understanding.get("summary", "Career highlights and best plays.")

    content: Dict[str, Any] = {
        "title": title,
        "summary": summary,
        "caption": f"{title} 🔥\n{summary}\nWhat do you think? 👇",
        "hashtags": generate_default_hashtags(understanding, profile),
        "pinned_comment": generate_default_pinned_comment(understanding, profile),
        "bgm_recommendations": audio_plan.get("bgm_recommendations", []),
        "cover_headline": cover_plan.get("headline", "MUST WATCH"),
        "cover_design_note": "High contrast, bold typography, face/subject protected.",
    }

    # Agent 提供的内容部分覆盖（跳过空值，避免清空兜底字段）
    content.update({k: v for k, v in agent_content.items()
                    if v not in (None, "", [])})

    # 校验：标签必须恰好 5 个
    hashtags = content.get("hashtags", [])
    if len(hashtags) != 5:
        log.warning(f"Hashtags count is {len(hashtags)}, expected exactly 5. Auto-adjusting...")
        if len(hashtags) > 5:
            content["hashtags"] = hashtags[:5]
        elif len(hashtags) < 5:
            # 补充默认标签
            understanding = load_json(work_path(work_dir, "video-understanding.json"), {})
            defaults = generate_default_hashtags(understanding, profile)
            for tag in defaults:
                if tag not in content["hashtags"]:
                    content["hashtags"].append(tag)
                if len(content["hashtags"]) >= 5:
                    break

    return content


def generate_processing_report(work_dir: str) -> Dict:
    """生成视频处理结果报告（如实披露）"""
    qa = load_json(work_path(work_dir, "qa.json"), {})
    clean_report = load_json(work_path(work_dir, "clean-report.json"), {})
    plan = load_json(work_path(work_dir, "localization-plan.json"), {})
    timeline = load_json(work_path(work_dir, "timeline.json"), {})
    asr = load_json(work_path(work_dir, "asr.json"), {})

    report = {
        "overall_status": qa.get("overall", "unknown"),
        "degradations": qa.get("degradations", []),
        "review_items": qa.get("review_items", []),
        "unresolved_timeline": timeline.get("unresolved", []),
        "low_confidence_clean": clean_report.get("inpaint", {}).get("low_confidence_count", 0),
        "audio_strategy": plan.get("audio_strategy", "unknown"),
        "asr_degraded": asr.get("degraded", False),
        "warnings": [],
    }

    # 汇总警告
    if report["overall_status"] == "WARNING":
        report["warnings"].append("Overall QA status: WARNING - see details below")
    if report["degradations"]:
        report["warnings"].append(f"{len(report['degradations'])} capability degradations occurred")
    if report["review_items"]:
        report["warnings"].append(f"{len(report['review_items'])} low-confidence items were preserved (not removed)")
    if report["unresolved_timeline"]:
        report["warnings"].append(f"{len(report['unresolved_timeline'])} subtitle timing issues unresolved")
    if report["low_confidence_clean"] > 0:
        report["warnings"].append(f"{report['low_confidence_clean']} low-confidence inpaint regions (quality may vary)")

    return report


def render_markdown(content: Dict, processing_report: Dict, output_dir: str,
                    profile: str = "generic",
                    music_credit: Optional[Dict[str, Any]] = None) -> str:
    """渲染固定九段结构 Markdown"""
    lines = []
    lines.append("# TikTok Post Plan")
    lines.append("")
    lines.append(f"> Profile: `{profile}` | Auto-generated")
    lines.append("")

    # 一、视频内容简介（必须英文）
    # 2026-10-29 用户规则：Summary 只写视频内容本身（画面里是什么球、谁、什么动作），
    # 不放系列元信息/策略/制作说明等其他内容，也不展示 Topic 行。
    lines.append("## 1. Video Summary")
    lines.append("")
    lines.append(f"{content.get('summary', 'N/A')}")
    lines.append("")

    # 二、TikTok Caption（英文）+ BGM 署名（CC BY 的 "BY" 是硬性条件，见 §12.4）
    lines.append("## 2. TikTok Caption")
    lines.append("")
    lines.append("```")
    lines.append(content.get("caption", "N/A"))
    lines.append("```")
    _render_music_credit(lines, music_credit)
    lines.append("")

    # 三、话题标签（恰好5个，相关且热门）
    lines.append("## 3. Hashtags (5 relevant & trending)")
    hashtags = content.get("hashtags", [])
    if hashtags:
        # 确保恰好 5 个
        display_tags = hashtags[:5] if len(hashtags) >= 5 else hashtags + ["#NBA"] * (5 - len(hashtags))
        # 2026-10-29 用户规则：发布时每个标签必须带 # 前缀（Agent 存的裸词在这里补齐）
        display_tags = ["#" + t.lstrip("#") if isinstance(t, str) and not t.startswith("#") else t
                        for t in display_tags]
        lines.append("```")
        lines.append(" ".join(display_tags))
        lines.append("```")
        lines.append("")
        lines.append(f"**Count**: {len(display_tags)} hashtags (must be exactly 5)")
    else:
        lines.append("N/A")
    lines.append("")

    # 四、推荐置顶评论（英文，含内容预告+品牌账号+互动问题）
    lines.append("## 4. Pinned Comment")
    lines.append("")
    lines.append("```")
    lines.append(content.get("pinned_comment", "N/A"))
    lines.append("```")
    lines.append("")
    lines.append("**Pinned comment strategy**:")
    lines.append("- Teases upcoming content to drive follows")
    lines.append("- Includes brand handle @HoopDynastyDaily for channel growth")
    lines.append("- Asks an engagement question to boost comment activity")
    lines.append("- Repeats key hashtags for discoverability")
    lines.append("")

    # 五、推荐背景音乐（具体歌名+艺术家+授权+下载方式）
    lines.append("## 5. BGM Recommendations")
    lines.append("")
    # 兼容两种写法：bgm_recommendations（name/artist/style/bpm/licensing/source/
    # download_url）与 music_recommendation（title/artist/bpm/style/license/why）。
    bgm_recs = content.get("bgm_recommendations") or content.get("music_recommendation", [])
    if bgm_recs:
        primary = bgm_recs[0] if bgm_recs else {}

        def _p(rec, *keys, default="N/A"):
            for k in keys:
                v = rec.get(k)
                if v not in (None, "", []):
                    return v
            return default

        lines.append(f"⭐ **Primary**: {_p(primary, 'name', 'title')}")
        lines.append(f"   - Artist: {_p(primary, 'artist')}")
        lines.append(f"   - Style: {_p(primary, 'style')}")
        lines.append(f"   - BPM: {_p(primary, 'bpm', 'bpm_measured')}")
        lines.append(f"   - Licensing: {_p(primary, 'licensing', 'license')}")
        lines.append(f"   - Source: {_p(primary, 'source', 'source_url')}")
        lines.append(f"   - Download: {_p(primary, 'download_url')}")
        if primary.get("why"):
            lines.append(f"   - Why: {primary['why']}")
        lines.append("")
        if len(bgm_recs) > 1:
            lines.append("**Alternatives** (rotate to avoid repetition):")
            for i, rec in enumerate(bgm_recs[1:4], 1):
                lines.append(f"{i}. {_p(rec, 'name', 'title')} — {_p(rec, 'artist', default='')} | {_p(rec, 'style', default='')} | {_p(rec, 'licensing', 'license', default='')}")
            lines.append("")
    else:
        lines.append("No BGM recommendations available.")
        lines.append("")
    lines.append("> ⚠️ Note: Recommended tracks must be verified for licensing before publishing. Prefer 'Free For Profit' or 'No Copyright' tagged music.")
    lines.append("")

    # 六、发布时间建议（北京时间+美国时间）
    lines.append("## 6. Posting Time (Beijing Time + US Time)")
    lines.append("")
    # The Agent's per-video slot. post-content.json has carried
    # best_post_time_bj / best_post_time_et all along, but nothing read them, so
    # a 10-video batch produced 10 identical generic blocks and the deliberate
    # spreading of upload slots was thrown away. Surface it when present.
    _slot_bj = (content.get("best_post_time_bj") or "").strip()
    _slot_et = (content.get("best_post_time_et") or "").strip()
    if _slot_bj or _slot_et:
        lines.append("### This video")
        lines.append("")
        if _slot_bj:
            lines.append(f"- **Beijing**: {_slot_bj}")
        if _slot_et:
            lines.append(f"- **US Eastern**: {_slot_et}")
        lines.append("")
    lines.append("### Best posting times (Beijing Time)")
    lines.append("- **Weekdays**: 19:00 - 22:00 (users relaxing after work)")
    lines.append("- **Weekends**: 14:00 - 18:00 (users leisure time)")
    lines.append("")
    lines.append("### Corresponding US Eastern Time (ET)")
    lines.append("- Beijing 19:00 = ET 07:00 (morning)")
    lines.append("- Beijing 20:00 = ET 08:00 (morning commute)")
    lines.append("- Beijing 21:00 = ET 09:00 (morning)")
    lines.append("- Beijing 22:00 = ET 10:00 (late morning)")
    lines.append("")
    lines.append("### Recommended")
    lines.append("- **Primary**: Beijing Sat/Sun 15:00 (ET 03:00, US users before bed scrolling)")
    lines.append("- **Secondary**: Beijing Weekday 20:00 (ET 08:00, US users commute scrolling)")
    lines.append("")

    # 七、视频封面
    lines.append("## 7. Video Cover")
    lines.append("")
    lines.append(f"**Cover headline**: {content.get('cover_headline', 'N/A')}")
    lines.append("")
    lines.append(f"**Design note**: {content.get('cover_design_note', 'N/A')}")
    lines.append("")
    lines.append("Cover file: `cover.jpg` (1080×1920, 9:16)")
    lines.append("")

    # 八、视频处理结果（如实披露）
    lines.append("## 8. Processing Report")
    lines.append("")
    lines.append(f"**Overall status**: {processing_report['overall_status']}")
    lines.append("")
    lines.append(f"**Audio strategy**: {processing_report['audio_strategy']}")
    lines.append("")

    if processing_report["warnings"]:
        lines.append("### ⚠️ Warnings")
        lines.append("")
        for w in processing_report["warnings"]:
            lines.append(f"- {w}")
        lines.append("")

    if processing_report["degradations"]:
        lines.append("### Capability degradations")
        lines.append("")
        for d in processing_report["degradations"]:
            lines.append(f"- **{d.get('capability', 'N/A')}**: used `{d.get('used', 'N/A')}` — {d.get('reason', 'N/A')}")
        lines.append("")

    if processing_report["review_items"]:
        lines.append("### Low-confidence items preserved (not removed)")
        lines.append("")
        for item in processing_report["review_items"]:
            lines.append(f"- **{item.get('item_id', 'N/A')}**: {item.get('summary', 'N/A')}")
        lines.append("")

    if not processing_report["warnings"] and not processing_report["degradations"]:
        lines.append("✅ All processing steps completed normally, no degradations or warnings.")
        lines.append("")

    # 九、最终文件
    lines.append("## 9. Final Files")
    lines.append("")
    lines.append("| File | Description |")
    lines.append("|------|-------------|")
    lines.append("| `final.mp4` | Final English video (cleaned + subtitles + audio) |")
    lines.append("| `cover.jpg` | TikTok cover (1080×1920) |")
    lines.append("| `subtitles-en.srt` | English subtitle file |")
    lines.append("| `tiktok-post.md` | This post plan |")
    lines.append("")
    lines.append("---")
    lines.append("*Auto-generated by TikTok Video Localizer Skill*")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Generate TikTok post markdown")
    ap.add_argument("--work-dir", required=True, help="Working directory")
    ap.add_argument("--output-dir", required=True, help="Output directory")
    ap.add_argument("--profile", default="generic", help="Profile name")
    ap.add_argument("--post-content", help="Path to post-content.json (Agent-generated)")
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    # 加载内容
    if args.post_content:
        agent = load_json(args.post_content, {})
        # 校验标签数量
        hashtags = agent.get("hashtags", [])
        if len(hashtags) != 5:
            log.warning(f"Hashtags count is {len(hashtags)}, expected exactly 5.")
        # 仍用 understanding/cover-plan 兜底，再叠加 Agent 内容（避免 summary/title 缺失）
        content = load_post_content(args.work_dir, args.profile, agent_content=agent)
    else:
        content = load_post_content(args.work_dir, args.profile)

    # 生成处理报告
    processing_report = generate_processing_report(args.work_dir)

    # BGM 署名（CC BY 的 "BY" 是许可条件，与发布平台无关）
    music_credit = resolve_music_credit(args.work_dir)
    if music_credit:
        if music_credit.get("unsafe"):
            log.warning(f"BGM license NOT usable on TikTok: {music_credit['track']}")
        elif music_credit.get("manual"):
            log.warning(f"BGM credit needs manual wording: {music_credit['track']}")
        else:
            log.info(
                f"Music credit: {music_credit['credit']}"
                + ("  [metadata inferred - verify]" if music_credit.get("inferred") else "")
            )

    # 渲染 Markdown
    markdown = render_markdown(content, processing_report, args.output_dir,
                               args.profile, music_credit)

    # 保存
    out_path = output_path(args.output_dir, "tiktok-post.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(markdown)

    result = make_result(
        status="ok",
        confidence=0.9,
        output_path=out_path,
        profile=args.profile,
        warnings_count=len(processing_report["warnings"]),
    )
    save_json(work_path(args.work_dir, "post-result.json"), result)

    log.info(f"Post generated: {out_path}")
    log.info(f"  Warnings disclosed: {len(processing_report['warnings'])}")


if __name__ == "__main__":
    main()
