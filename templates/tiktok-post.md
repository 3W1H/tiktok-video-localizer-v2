# TikTok 发布方案

> Profile: `{profile}` | 自动生成 | {date}

> ⚠️ **本文件只是结构与措辞的参考样本，没有任何代码读取它。**
> 真正的输出由 `scripts/generate_post.py::render_markdown()` 生成 ——
> 改结构/字段请改那里，别改这里（本 skill 反复栽在"两个真源"上）。
> 下面的标题与脚本实际产出保持一致（英文序号 + 英文小节名），**别改回中文序号**
> —— 早先这文件写的是一~八段中文编号，与真实输出九段不符，属于误导。

## 1. Video Summary

**视频主题**：{title}

**视频简介**：{summary}

## 2. TikTok Caption

{caption}

> Music: {music_title} by {music_artist} ({music_source}), licensed under Creative Commons: By Attribution {music_version}
>
> ⭐ **署名行由脚本自动生成**（`resolve_music_credit()`），CC BY 的 "BY" 是**许可条件**、
> 与发布平台无关 —— 只发 TikTok 也不能省（SKILL.md §12.4 第 6 条）。
> 许可为 `YouTube Audio Library License` 时不生成署名，直接出「授权不可用」警告。

## 3. Hashtags (5 relevant & trending)

{hashtags}

## 4. Pinned Comment

{pinned_comment}

## 5. BGM Recommendations

⭐ **首选**：{bgm_primary_name}
   - 风格：{bgm_primary_style}
   - 授权状态：{bgm_primary_licensing}
   - 类型：{bgm_primary_type}

**备选**：
{bgm_alternatives}

> ⚠️ 注意：推荐曲目不等于可自由使用，发布前请确认授权状态。
> {bgm_asset_note}

## 6. Posting Time (Beijing Time + US Time)

{posting_time}

## 7. Video Cover

**Cover 文案**：{cover_headline}

**设计说明**：{cover_design_note}

封面文件：`cover.jpg`（1080×1920，9:16）

## 8. Processing Report

**整体状态**：{qa_overall}

**音频策略**：{audio_strategy}

{processing_warnings}

{degradation_records}

{review_items}

## 9. Final Files

| 文件 | 说明 |
|------|------|
| `final.mp4` | 最终英文视频（已清理+字幕+音频） |
| `cover.jpg` | TikTok 封面（1080×1920） |
| `subtitles-en.srt` | 英文字幕文件（**只出 SRT，不烧录**） |
| `tiktok-post.md` | 本发布方案 |

---
*由 TikTok Video Localizer Skill 自动生成*
