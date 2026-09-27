# Job: analyze-video

## 目标
分析输入视频，产出完整证据包和 localization-plan.json（枢纽契约）。

## 输入
- `input/source.mp4`（必需）
- `--profile`（可选，默认 generic）
- `--review`（可选）

## 输出
- `work/probe.json`
- `work/frames-index.json`
- `work/ocr.json`（两遍合并）
- `work/text-regions.json`
- `work/dense-windows.json`
- `work/text-tracks.json`
- `work/asr.json`
- `work/video-understanding.json`（Agent 产出）
- `work/localization-plan.json`（Agent 产出，★枢纽契约）

## 步骤

### Step 1: Probe 视频元数据
```bash
python scripts/video_probe.py --video input/source.mp4 --work-dir work
```
- 确定性脚本，无 Agent 介入
- 产出：`work/probe.json`

### Step 2: 第一层粗采样（1FPS + 场景变化）
```bash
python scripts/extract_frames.py --video input/source.mp4 --work-dir work
```
- 确定性脚本
- 产出：`work/frames/coarse/` + `work/frames/scene/` + `work/frames-index.json`

### Step 3: OCR 第一遍（粗扫帧）
```bash
python scripts/ocr_video.py --work-dir work --pass coarse
```
- 确定性脚本（本地 OCR：RapidOCR，唯一默认引擎，不降级）
- 产出：`work/ocr.json`（coarse pass）

### Step 4: 检测文字区域 + 产出疑似文字窗口
```bash
python scripts/detect_text_regions.py --work-dir work --emit-dense-windows
```
- 确定性脚本，只产证据包，不做分类
- 产出：`work/text-regions.json` + `work/dense-windows.json`

### Step 5: 第二层局部加密采样（仅疑似文字窗口）
```bash
python scripts/extract_frames.py --video input/source.mp4 --work-dir work \
    --dense-windows work/dense-windows.json
```
- 确定性脚本，仅对疑似窗口加密（5FPS），不全视频高帧率
- 产出：`work/frames/dense/` + 更新 `work/frames-index.json`

### Step 6: OCR 第二遍（加密帧）
```bash
python scripts/ocr_video.py --work-dir work --pass dense
```
- 确定性脚本
- 产出：更新 `work/ocr.json`（合并两遍）

### Step 7: 文字跟踪（跨帧关联）
```bash
python scripts/track_text.py --work-dir work
```
- 确定性脚本，产出 start/end/position/persistence/trajectory/asr_text_overlap
- **注意**：asr_text_overlap 只是词汇重叠，不是语义；semantic_match 由 Agent 填写
- 产出：`work/text-tracks.json`

### Step 8: ASR 语音转写
```bash
python scripts/audio_processor.py asr-only --video input/source.mp4 --work-dir work
```
- 确定性脚本（Whisper local → fallback）
- 产出：`work/asr.json`（含 `has_chinese_narration`）

### Step 9: [AGENT] 视频理解
- 使用 `prompts/video_understanding.md`
- 输入：probe 摘要 + 采样帧 + OCR 摘要 + ASR 摘要
- 产出：`work/video-understanding.json`

### Step 10: [AGENT] 文字分类 + Confidence 评估
- 使用 `prompts/classify_text.md`
- 输入：text-tracks.json + ASR transcript + video-understanding.json + profile
- 对每个文字区域：category（9类）+ action（5种：keep/translate/remove/replace/review）+ confidence + reason
- Confidence 三档：≥0.85 自动 / 0.60-0.85 执行+WARNING / <0.60 → action=review
- **translate 必须包含 source_text_removal: true**（移除原中文字幕+背景修复）
- **replace 必须有 replacement.asset**，默认 fit: contain（保持宽高比，禁止拉伸）
- 产出：分类结果

### Step 11: [AGENT] 音频策略决策（规则化）
- `asr.has_chinese_narration = true` → `audio_strategy = mute_original_tts_bgm`
- `asr.has_chinese_narration = false` → `audio_strategy = keep_original`
- ASR 不可用 → 保守走 `keep_original` + WARNING

### Step 12: [AGENT] 写 localization-plan.json
- ★ 枢纽契约：Agent 写、Scripts 读
- 包含：profile / review_mode / audio_strategy / bgm / items[]
- 产出：`work/localization-plan.json`

### Step 13: [AGENT] Review 模式（如果 --review 或存在 action=review 项）
- 使用 `prompts/review_report.md`
- 向用户输出报告（编号+时间点+区域描述+建议）
- 等待用户裁决后更新 localization-plan.json
- 产出：`work/review-report.json`

## 异常处理
- 任一脚本退出码 2（致命）→ 停止，上报用户
- 任一脚本退出码 1（可降级）→ 记录 warnings，继续，最终 QA 汇总
- OCR 不可用 → 文字类处理跳过，仅走 ASR 字幕翻译路径
- ASR 不可用 → 保守 keep_original + WARNING
