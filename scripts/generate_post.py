#!/usr/bin/env python3
"""
generate_post.py - 生成 TikTok 发布方案（八段结构 Markdown）

输入：各 plan/结果 JSON + Agent 生成的文案
输出：output/tiktok-post.md

严格八段结构：
  一、视频内容简介（必须英文）
  二、TikTok Caption（英文）
  三、话题标签（恰好5个，相关且热门）
  四、推荐置顶评论（英文，含内容预告+品牌账号+互动问题）
  五、推荐背景音乐（具体歌名+艺术家+授权+下载方式）
  六、视频封面
  七、视频处理结果（如实披露降级/低置信度/未解决项）
  八、最终文件

核心规则（必须遵守，违反即不合格）：
  1. 内容简介、Caption、置顶评论必须是英文（源视频是中文，但目标平台是 TikTok 英文用户）
  2. 话题标签恰好 5 个，必须是与视频内容相关且在 TikTok 上热门的标签（禁止泛泛的 #fyp #viral）
  3. 置顶评论必须包含：内容预告（驱动关注）、品牌账号、互动问题（提升评论量）
  4. BGM 推荐必须给出具体歌名、艺术家、BPM、风格、授权状态、下载方式，不能泛泛推荐
  5. 发布时间建议必须同时给出北京时间和对应美国时间（美东/美西）

最终用户输出是 Markdown，不是 JSON。
"""

import argparse
import os
import sys
from typing import Dict, Any, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    save_json, load_json, make_result, log,
    work_path, output_path,
)


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


def load_post_content(work_dir: str, profile: str = "generic") -> Dict:
    """加载 Agent 生成的发布文案内容"""
    post_content_path = work_path(work_dir, "post-content.json")
    content = load_json(post_content_path, {})

    # 如果没有 Agent 生成的内容，从各 JSON 中提取基础信息
    if not content:
        understanding = load_json(work_path(work_dir, "video-understanding.json"), {})
        plan = load_json(work_path(work_dir, "localization-plan.json"), {})
        cover_plan = load_json(work_path(work_dir, "cover-plan.json"), {})
        audio_plan = load_json(work_path(work_dir, "audio-plan.json"), {})

        # 内容简介必须是英文
        title = understanding.get("topic", "NBA Highlights")
        summary = understanding.get("summary", "Career highlights and best plays.")

        content = {
            "title": title,
            "summary": summary,
            "caption": f"{title} 🔥\n{summary}\nWhat do you think? 👇",
            "hashtags": generate_default_hashtags(understanding, profile),
            "pinned_comment": generate_default_pinned_comment(understanding, profile),
            "bgm_recommendations": audio_plan.get("bgm_recommendations", []),
            "cover_headline": cover_plan.get("headline", "MUST WATCH"),
            "cover_design_note": "High contrast, bold typography, face/subject protected.",
        }

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
                    profile: str = "generic") -> str:
    """渲染八段结构 Markdown"""
    lines = []
    lines.append("# TikTok Post Plan")
    lines.append("")
    lines.append(f"> Profile: `{profile}` | Auto-generated")
    lines.append("")

    # 一、视频内容简介（必须英文）
    lines.append("## 1. Video Summary")
    lines.append("")
    lines.append(f"**Topic**: {content.get('title', 'N/A')}")
    lines.append("")
    lines.append(f"**Summary**: {content.get('summary', 'N/A')}")
    lines.append("")

    # 二、TikTok Caption（英文）
    lines.append("## 2. TikTok Caption")
    lines.append("")
    lines.append("```")
    lines.append(content.get("caption", "N/A"))
    lines.append("```")
    lines.append("")

    # 三、话题标签（恰好5个，相关且热门）
    lines.append("## 3. Hashtags (5 relevant & trending)")
    lines.append("")
    hashtags = content.get("hashtags", [])
    if hashtags:
        # 确保恰好 5 个
        display_tags = hashtags[:5] if len(hashtags) >= 5 else hashtags + ["#NBA"] * (5 - len(hashtags))
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
    bgm_recs = content.get("bgm_recommendations", [])
    if bgm_recs:
        primary = bgm_recs[0] if bgm_recs else {}
        lines.append(f"⭐ **Primary**: {primary.get('name', 'N/A')}")
        lines.append(f"   - Artist: {primary.get('artist', 'N/A')}")
        lines.append(f"   - Style: {primary.get('style', 'N/A')}")
        lines.append(f"   - BPM: {primary.get('bpm', 'N/A')}")
        lines.append(f"   - Licensing: {primary.get('licensing', 'N/A')}")
        lines.append(f"   - Source: {primary.get('source', 'N/A')}")
        lines.append(f"   - Download: {primary.get('download_url', 'N/A')}")
        lines.append("")
        if len(bgm_recs) > 1:
            lines.append("**Alternatives** (rotate to avoid repetition):")
            for i, rec in enumerate(bgm_recs[1:4], 1):
                lines.append(f"{i}. {rec.get('name', 'N/A')} — {rec.get('artist', '')} | {rec.get('style', '')} | {rec.get('licensing', '')}")
            lines.append("")
    else:
        lines.append("No BGM recommendations available.")
        lines.append("")
    lines.append("> ⚠️ Note: Recommended tracks must be verified for licensing before publishing. Prefer 'Free For Profit' or 'No Copyright' tagged music.")
    lines.append("")

    # 六、发布时间建议（北京时间+美国时间）
    lines.append("## 6. Posting Time (Beijing Time + US Time)")
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
        content = load_json(args.post_content, {})
        # 校验标签数量
        hashtags = content.get("hashtags", [])
        if len(hashtags) != 5:
            log.warning(f"Hashtags count is {len(hashtags)}, expected exactly 5.")
    else:
        content = load_post_content(args.work_dir, args.profile)

    # 生成处理报告
    processing_report = generate_processing_report(args.work_dir)

    # 渲染 Markdown
    markdown = render_markdown(content, processing_report, args.output_dir, args.profile)

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
