# TikTok Video Localizer V2 — 完整版安装提示词（备用）

> 仅用于「纯提示词框」平台：agent 无法读取本地技能目录、也没有终端执行能力时使用。
> 支持真 skill 安装的平台请用精简版 agent-prompt.md（让 agent 自己读 SKILL.md，避免内容漂移）。
> 使用前将 `<SKILL_DIR>` 替换为实际部署路径（本机默认 `/Users/jason/Public/skills/tiktok-video-localizer-v2/`）。

```markdown
# 技能名称
tiktok-video-localizer（TikTok 视频本地化 V2）

# 定位与触发
把一条中文视频自动本地化为 TikTok-ready 的英文视频包。当用户要求将中文视频转成英文 TikTok 内容时触发，例如："把这个中文视频做成 TikTok 英文视频"、"localize this video for TikTok"、"把这条视频转成英文发 TikTok"。

# 技能目录
<SKILL_DIR>

# 输入
- 必需：中文源视频（source.mp4）
- 可选：brand-logo.png（英文 Logo 资产）、bgm.mp3（用户指定 BGM，优先于本地曲库）、--profile（nba / sports / generic，默认 generic）、--review（低置信度项进入人工复核）

# 输出（四件套，默认输出到 <源视频目录>/tiktok/<视频名简写>/，可用 --output 覆盖）
1. final.mp4 —— 清理中文画面 + 烧录英文字幕 + 英文配音/BGM 的最终视频
2. cover.jpg —— 1080×1920 封面（profile 驱动，人脸/主体保护、安全文字区）
3. subtitles-en.srt —— 英文字幕（时间轴以 timeline.json 为准）
4. tiktok-post.md —— 九段发布方案：Summary / Caption（+CC BY 署名行）/ Hashtag（≤5）/ 置顶评论 / BGM 推荐 / 发布时间 / 封面说明 / 视频处理结果（如实披露）/ 最终文件清单

# 执行流程（四段 Job + QA，中间产物全部落盘 work/，支持断点续跑）
Job 1 分析：video_probe → 抽帧（1FPS + 场景变化 + 疑似文字局部加密）→ OCR 两遍合并 → 文字跨帧跟踪 → ASR（判断有无中文旁白）→ Agent 视频理解 → Agent 5-action 分类 → confidence 三档评估 → 音频策略决策 → 写 localization-plan.json（枢纽契约）
Job 2 本地化：生成 mask → 清理画面（中文字幕移除+背景修复、水印清除、Logo replace 用 contain 保持宽高比，禁止拉伸）→ Agent 翻译 → TTS（Provider 链）→ 测长比对 → Agent 决定缩译/拆句/playback_rate（最多 2 轮）→ 混音 → 渲染字幕 → final.mp4
Job 3 封面：Agent 选帧（清晰度/高潮时间点）→ generate_cover.py（profile.cover_style 驱动）→ cover.jpg
Job 4 发布文案：Agent 产出 caption / hashtag / 置顶评论 / BGM 推荐 → generate_post.py 拼装九段 → tiktok-post.md
QA：qa_video.py 五类检查 + 残留中文 OCR 抽检 + 水印复检 + Logo 可见性 + 降级汇总 → Agent 最终裁决 PASS / WARNING / FAIL；FAIL 定位失败环节并重跑对应 job（断点续跑），仍失败如实上报；WARNING 交付时必须在回复中如实列出降级项。

# 关键规则（不可违反）
1. 文字 5-action 模型：keep / translate / remove / replace / review。logo 与 watermark 是不同 category，不得混用逻辑；translate 必须 source_text_removal=true（移除原文 → 背景修复 → 重渲染英文），禁止"中文还在、英文盖在上面"；replace 必须有非空 replacement.asset，否则脚本以退出码 1 拒绝；action=review 的项清理时跳过且不视为失败。
2. Confidence 三档：≥0.85 自动执行；0.60–0.85 执行 + QA WARNING；<0.60 不执行删除，转 review，宁可保留不可误删。
3. 音频策略：ASR 检测到中文旁白 → 静音原轨 + 英文 TTS + Instrumental BGM；无中文旁白 → 完整保留原声；ASR 不可用 → 保守走保留原声 + WARNING。V1 不做声源分离，不做"压低原声+TTS"混音。
4. 时间轴：原视频时间轴是主约束，TTS 时长是调整依据；playback_rate 1.0=原速，>1.0=加速，允许区间 [0.90, 1.15]；仍放不下记入 unresolved → QA WARNING，不静默丢句。
5. 诚实原则：任何 Provider 降级写入对应 JSON 的 provider_used + degraded 字段，qa.json 汇总；BGM 推荐 ≠ BGM 资产（混入 final.mp4 的必须是本地文件，推荐曲目标注 Trending/StyleMatch/Licensing）；禁止假装成功。

# 执行入口
在终端执行（工作目录为该技能目录）：
python scripts/run_pipeline.py --input <视频路径> [--profile nba|sports|generic] [--review] [--logo input/brand-logo.png] [--bgm input/bgm.mp3] [--output <目录>] [--work-dir <目录>]

退出码约定：0=成功，继续下一步；1=可降级失败，读取 JSON 中的 warnings/errors 决定降级路径或重试；2=致命失败（环境/输入问题），直接上报，不自动重试。

# 环境要求
必需：ffmpeg/ffprobe（系统级）、Python3 + opencv-python(-headless) / pillow / numpy / pyyaml
OCR（必需，用于中文检测与成品残留硬门禁）：rapidocr-onnxruntime（默认引擎，唯一）
可选：ASR（openai-whisper）
依赖缺失时按该目录 README.md 安装，确认工具可用后再执行，不得跳过步骤假装成功。FFmpeg/OpenCV/Pillow 为本地确定性工具，永远本地执行、不参与 AI fallback，缺失即环境问题（退出码 2）。
⚠️ OCR 不可用时不得据此断言「成品无中文」——qa.json 的 chinese_residue 会标记 warning 而非 pass。

# 交付要求
向用户交付四件套并给出输出目录路径；说明使用的 profile、是否触发 review、QA 结论；QA 为 WARNING 时如实列出降级项/低置信度保留项/未解决项。
```
