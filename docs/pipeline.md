# Pipeline - 完整执行流程

## 总览

```
Source Video
    ↓
Video Probe
    ↓
Adaptive Frame Sampling (4 layers)
    ↓
OCR (2 passes)
    ↓
ASR
    ↓
Text Tracking
    ↓
[Agent] Video Understanding
    ↓
[Agent] Text Classification (5-action)
    ↓
[Agent] Confidence Evaluation (3 tiers)
    ↓
Localization Plan (★ hub contract)
    ↓
┌──────────────────────────────┐
│ Video Processing             │
│                              │
│ subtitle → translate         │
│ title → translate/remove     │
│ watermark → remove           │
│ logo → remove/replace        │
└──────────────────────────────┘
    ↓
Audio Decision
    ↓
┌──────────────────────────────┐
│ 有中文旁白                    │
│      ↓                       │
│ 默认移除原音                  │
│      ↓                       │
│ English TTS                  │
│      +                       │
│ Instrumental BGM             │
└──────────────────────────────┘
    ↓
English Subtitle (timeline.json)
    ↓
Cover Generation
    ↓
TikTok Caption / Hashtags / Pinned Comment / BGM Recommendation
    ↓
QA (5 categories + residual Chinese check)
    ↓
Final TikTok Package
```

## Job 1: analyze-video

### Step 1: Probe
```bash
python scripts/video_probe.py --video input/source.mp4 --work-dir work
```
- 输出：`work/probe.json`

### Step 2: 粗采样（第一层 + 第二层）
```bash
python scripts/extract_frames.py --video input/source.mp4 --work-dir work
```
- 第一层：1FPS 定时采样
- 第二层：场景变化帧
- 输出：`work/frames/coarse/` + `work/frames/scene/` + `work/frames-index.json`

### Step 3: OCR 第一遍
```bash
python scripts/ocr_video.py --work-dir work --pass coarse
```
- 本地 OCR：RapidOCR（唯一默认引擎，不做静默降级）
- 输出：`work/ocr.json`

### Step 4: 检测文字区域 + 疑似窗口
```bash
python scripts/detect_text_regions.py --work-dir work --emit-dense-windows
```
- 聚合 OCR 检测，产出疑似文字窗口
- 输出：`work/text-regions.json` + `work/dense-windows.json`

### Step 5: 局部加密采样（第三层）
```bash
python scripts/extract_frames.py --video input/source.mp4 --work-dir work \
    --dense-windows work/dense-windows.json
```
- 仅对疑似文字窗口加密（5FPS），不全视频高帧率
- 输出：`work/frames/dense/` + 更新 `work/frames-index.json`

### Step 6: OCR 第二遍
```bash
python scripts/ocr_video.py --work-dir work --pass dense
```
- 输出：更新 `work/ocr.json`（合并两遍）

### Step 7: 文字跟踪（第四层：确认时间）
```bash
python scripts/track_text.py --work-dir work
```
- 跨帧关联，产出 start/end/position/persistence/trajectory/asr_text_overlap
- 输出：`work/text-tracks.json`

### Step 8: ASR
```bash
python scripts/audio_processor.py asr-only --video input/source.mp4 --work-dir work
```
- Whisper local → fallback
- 输出：`work/asr.json`（含 `has_chinese_narration`）

### Step 9: [Agent] 视频理解
- Prompt：`prompts/video_understanding.md`
- 输出：`work/video-understanding.json`

### Step 10: [Agent] 文字分类 + Confidence
- Prompt：`prompts/classify_text.md`
- 5-action：keep/translate/remove/replace/review
- 9-category：spoken_subtitle/title/caption_overlay/watermark/logo/ad/scene_text/background_text/other
- Confidence 三档：≥0.85 自动 / 0.60-0.85 WARNING / <0.60 review

### Step 11: [Agent] 音频策略决策
- `has_chinese_narration=true` → `mute_original_tts_bgm`
- `has_chinese_narration=false` → `keep_original`
- ASR 不可用 → 保守 `keep_original`

### Step 12: [Agent] 写 localization-plan.json
- ★ 枢纽契约
- 输出：`work/localization-plan.json`

### Step 13: [AGENT] Review 模式（可选）
- 如果 `--review` 或存在 action=review 项
- Prompt：`prompts/review_report.md`
- 向用户输出报告，等待裁决
- 输出：`work/review-report.json`

## Job 2: localize-video

### Step 1: 生成 Masks
```bash
python scripts/generate_masks.py --work-dir work --render-images
```
- 为 remove/replace/translate(需要 removal) 区域生成时序 mask
- 人脸/主体保护冲突检查
- 输出：`work/masks/masks-index.json`

### Step 2: 清理视频
```bash
python scripts/clean_video.py --video input/source.mp4 --work-dir work
```
- 清理顺序：crop → clone_patch → OpenCV inpaint
- translate 项：移除原中文字幕 + 背景修复
- replace 项：inpaint + Pillow 贴合用户 Logo（contain 保持宽高比）
- 拒绝执行 action=review
- 输出：`work/cleaned.mp4` + `work/clean-report.json`

### Step 3: [Agent] 翻译
- Prompt：`prompts/translate.md`
- 带时间窗口约束，TikTok 原生英文
- 输出：`work/translated.json`

### Step 4: TTS 合成
```bash
python scripts/audio_processor.py tts --work-dir work
```
- Provider 链：Agent Native → Cloud → local_tts_cli → skip
- 输出：`work/tts/` + `work/tts-result.json`

### Step 5: TTS 时间轴测量（Script 只测量）
```bash
python scripts/tts_timing.py measure --work-dir work
```
- 测量 TTS 时长，与原窗口比对，报告 overflow/underflow
- **不做决策**
- 输出：`work/timing-report.json`

### Step 6: [Agent] TTS 调整决策
- Prompt：`prompts/tts_adjust.md`
- 方法优先级：shorten_translation → resegment → speed_up → shift_into_gap → combine
- playback_rate 区间 [0.90, 1.15]
- 最多 2 轮
- 输出：`work/timing-decisions.json`

### Step 7: 执行时间轴调整（Script 执行 Agent 决策）
```bash
python scripts/tts_timing.py apply --work-dir work
```
- 执行决策，复检，unresolved → QA WARNING
- 输出：`work/timeline.json`（★ 字幕与混音的唯一时间基准）

### Step 8: 混音
```bash
python scripts/audio_processor.py mix --video input/source.mp4 \
    --work-dir work --output-dir output
```
- mute_original_tts_bgm：静音原轨 + TTS（按 timeline）+ BGM Asset + 响度归一化
- keep_original：完整保留原声
- 输出：`work/audio-plan.json` + `output/final.mp4`

### Step 9: 渲染字幕
```bash
python scripts/render_subtitles.py --work-dir work --output-dir output \
    --video output/final.mp4 --burn
```
- 以 timeline.json 为准生成 SRT + 烧录
- 输出：`output/subtitles-en.srt` + 更新 `output/final.mp4`

## Job 3: generate-cover

### Step 1: [Agent] 选帧 + 文案
- 候选帧：清晰度 + 高潮时间点
- Prompt：`prompts/cover_copy.md`（3-5 词英文 Hook）

### Step 2: 渲染封面
```bash
python scripts/generate_cover.py --work-dir work --output-dir output --profile nba
```
- Profile 驱动样式（字体/对比度/词数/主体保护）
- 人脸检测 → 安全文字区 → 排版渲染
- 输出：`output/cover.jpg` + `work/cover-plan.json`

## Job 4: generate-post

### Step 1: [Agent] 文案生成
- Caption：`prompts/tiktok_caption.md`
- Hashtags：`prompts/hashtag.md`（≤5）
- Pinned Comment：`prompts/pinned_comment.md`
- BGM Recommendation：`prompts/music_recommendation.md`（标注 Licensing）

### Step 2: 拼装 tiktok-post.md
```bash
python scripts/generate_post.py --work-dir work --output-dir output --profile nba
```
- 八段结构 Markdown
- 第七段如实披露降级/低置信度/未解决项
- 输出：`output/tiktok-post.md`

## QA

```bash
python scripts/qa_video.py --work-dir work --output-dir output
```
- 五类检查：video / subtitle / cover / audio / localization
- 额外：残留中文 OCR 抽检、水印区复检、replace 后 Logo 可见性、review 项汇总
- 输出：`work/qa.json`（PASS / WARNING / FAIL）

### [Agent] 最终裁决
- PASS → 交付四件套
- WARNING → 交付 + 如实列出降级项/低置信度保留项/timeline 未解决项
- FAIL → 定位失败环节，修复重跑该 job（断点续跑），仍失败则如实上报

## 最终输出

```
output/
├── final.mp4           # 最终英文视频
├── cover.jpg           # TikTok 封面（1080×1920）
├── subtitles-en.srt    # 英文字幕
└── tiktok-post.md      # 发布方案（八段 Markdown）
```
