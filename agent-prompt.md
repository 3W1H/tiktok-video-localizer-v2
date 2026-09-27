# 技能名称
tiktok-video-localizer（TikTok 视频本地化 V2）

# 触发条件
用户要求把中文视频本地化为 TikTok 英文内容时触发，例如："把这个中文视频做成 TikTok 英文视频"、"localize this video for TikTok"、"把这条视频转成英文发 TikTok"。

# 技能位置
/Users/jason/Public/skills/tiktok-video-localizer-v2/
（软链入口：/Users/jason/Doubao/skills/tiktok-video-localizer，指向同一目录；若部署到其他机器，替换为对应路径）

# 使用方式
1. 触发后先完整读取 /Users/jason/Public/skills/tiktok-video-localizer-v2/SKILL.md；流程细节按需读取 jobs/*.job.md、prompts/*.md、profiles/*.yaml，一切以该目录内文件为准。
2. 按 SKILL.md 的主流程执行：视频分析 → 画面本地化（清理+翻译+字幕+TTS/混音）→ 封面 → 发布文案 → QA，输出四件套（final.mp4 / cover.jpg / subtitles-en.srt / tiktok-post.md）并交付给用户。
3. 确定性步骤通过终端调用 scripts/ 下脚本（入口：python scripts/run_pipeline.py --input <视频> [--profile nba|sports|generic] [--review] [--logo ...] [--bgm ...]），遵守退出码约定与 SKILL.md 中的关键规则（5-action 模型、confidence 三档、音频策略、时间轴、诚实原则）。
4. 若当前平台无法访问该目录或无法执行脚本，按 SKILL.md 的降级原则如实处理，不假装完成。
