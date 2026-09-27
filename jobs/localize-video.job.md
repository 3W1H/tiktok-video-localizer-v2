# Job: localize-video

## 目标
根据 localization-plan.json 执行视频清理、翻译、TTS、时间轴对齐、混音、字幕渲染，产出 final.mp4。

## 输入
- `work/localization-plan.json`（枢纽契约）
- `work/probe.json`
- `work/cleaned.mp4`（产出）
- `input/brand-logo.png`（可选，用于 logo replace）
- `input/bgm.mp3`（可选，用户指定 BGM）

## 输出
- `work/masks/masks-index.json`
- `work/cleaned.mp4`
- `work/clean-report.json`
- `work/translated.json`（Agent 产出）
- `work/tts/`（TTS 音频文件）
- `work/timing-report.json`
- `work/timing-decisions.json`（Agent 产出）
- `work/timeline.json`（最终时间轴）
- `work/audio-plan.json`
- `output/final.mp4`
- `output/subtitles-en.srt`

## 步骤

### Step 1: 生成 Masks
```bash
python scripts/generate_masks.py --work-dir work --render-images
```
- 为 action in [remove, replace, translate(需要 removal)] 的区域生成时序 mask
- 人脸/主体保护冲突检查
- action=review 的项跳过
- 产出：`work/masks/masks-index.json`

### Step 2: 清理视频（inpaint + Logo replace）
```bash
python scripts/clean_video.py --video input/source.mp4 --work-dir work
```
- 清理顺序：crop → clone_patch → OpenCV inpaint → [可选 AI inpaint]
- **translate 项**：移除原中文字幕 + 背景修复（禁止"中文还在、英文盖在上面"）
- **replace 项**：inpaint 后用 Pillow 贴合用户 Logo，默认 `fit: contain`（保持宽高比，禁止拉伸变形）
- **拒绝执行 action=review**（不视为失败）
- **replace 必须有 replacement.asset**，否则退出码 1 拒绝
- 产出：`work/cleaned.mp4` + `work/clean-report.json`（如实标注 LOW confidence 区域）

### Step 3: [AGENT] 翻译（带时间窗口约束）
- 使用 `prompts/translate.md`
- 输入：localization-plan 中 action=translate 的项 + 每项的 window_sec（原时间窗口）
- 输出 TikTok 原生英文（非直译），保留语气情绪
- 每段给出 length_estimate（预估 TTS 时长）和 length_ok（是否在窗口内）
- 产出：`work/translated.json`

### Step 4: TTS 合成
```bash
python scripts/audio_processor.py tts --work-dir work
```
- 对 translated.json 中的每段生成英文 TTS 音频
- Provider 链：Agent Native → Cloud TTS → local_tts_cli → skip_with_warning
- TTS 完全不可用时：静音原轨 + 无配音 + 英文字幕 + BGM → WARNING
- 产出：`work/tts/tts_XXXX.wav` + `work/tts-result.json`

### Step 5: TTS 时间轴测量（Script 只测量，不决策）
```bash
python scripts/tts_timing.py measure --work-dir work
```
- 测量每段 TTS 实际时长
- 与原时间窗口比对，报告 overflow / underflow
- **不做任何调整决策**（决策是 Agent 的职责）
- 产出：`work/timing-report.json`（含 needs_agent_decision 列表）

### Step 6: [AGENT] TTS 调整决策
- 使用 `prompts/tts_adjust.md`
- 对 overflow 项决定调整方法：
  1. shorten_translation（缩短译文，最优先）
  2. resegment（重新分句）
  3. speed_up（提高 playback_rate，最多 1.15）
  4. shift_into_gap（挪入相邻间隙）
  5. combine（合并相邻短句）
- **playback_rate 统一定义**：1.0=原速，>1.0=加速（1.10=+10%），<1.0=降速，区间 [0.90, 1.15]
- 最多迭代 2 轮
- 产出：`work/timing-decisions.json`

### Step 7: 执行时间轴调整（Script 执行 Agent 决策）
```bash
python scripts/tts_timing.py apply --work-dir work
```
- 执行 timing-decisions.json 中的方案
- 复检调整后是否 fits 窗口
- 仍放不下的记入 unresolved → QA WARNING（不静默丢句）
- 产出：`work/timeline.json`（★ 字幕与混音的唯一时间基准）

### Step 8: 混音
```bash
python scripts/audio_processor.py mix --video input/source.mp4 \
    --work-dir work --output-dir output
```
- 按 audio_strategy：
  - `mute_original_tts_bgm`：静音原轨 + 英文 TTS（按 timeline 放置）+ BGM Asset（用户提供或本地曲库）+ 响度归一化
  - `keep_original`：完整保留原声
- BGM 推荐 ≠ BGM Asset：混入的必须是本地音频文件
- 无 BGM 时：纯 TTS，post.md 如实写"未混入 BGM"
- 产出：`work/audio-plan.json` + `output/final.mp4`（含音频）

### Step 9: 渲染字幕（以 timeline.json 为准）
```bash
python scripts/render_subtitles.py --work-dir work --output-dir output \
    --video output/final.mp4 --burn
```
- 从 timeline.json 生成 SRT（不直接用 translated.json）
- 烧录字幕到视频（样式来自 subtitle-style.json，可被 profile 覆盖）
- 产出：`output/subtitles-en.srt` + 更新 `output/final.mp4`

## 异常处理
- clean_video 退出码 1（replace 缺资产）→ Agent 降级该项为 remove 或 review
- TTS 不可用 → 静音原轨 + 字幕 + BGM → WARNING
- timeline unresolved → QA WARNING，不静默丢句
- 清理低置信度区域 → clean-report 标 LOW，QA 抽检残留中文
