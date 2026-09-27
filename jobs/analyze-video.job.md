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
- `work/rhythm.json`（★ 视频节奏分析，2026-09-28 新增）
- `work/narration-style.json`（★ 解说声学风格，2026-09-28 新增）
- `work/bgm-plan.json`（★ BGM 实测 BPM 与高潮对齐，2026-09-28 新增）
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
- ⚠️ **`has_chinese_narration` 不可直接采信**：whisper 会把非语音音频幻觉成中文。
  先看 `segments[].avg_logprob`——**≤ −3、或文本混着其他语种（俄/韩字母）、或不连贯碎片 = 幻觉**，
  按"非旁白"处理（见 SKILL.md §14.2）。2026-09-27 批次 8 条里 5 条中招。
- ⭐ **2026-09-28 起用脚本自动判**：`analyze_narration.py` 里的
  `detect_asr_hallucination()` —— 三条判据（平均 avg_logprob < −0.70 / 重复句占比 ≥40% /
  固定时长段占比 ≥60%）**命中两条**即判幻觉。实测 01（奥尼尔·艾佛森）三条全中：
  16 段里 15 段都是整 2.00s、文本循环「我來做個高度／今天在吃飯」、平均 avg_logprob −0.81。

### Step 9: 视频节奏分析（★ 2026-09-28 新增）
```bash
python scripts/analyze_rhythm.py --video input/source.mp4 --work-dir work
```
- 确定性脚本。量两件事：**场景切换点**（ffmpeg scene detect）+ **逐帧运动强度**（像素差）
- 产出：`work/rhythm.json`（`cuts` / `motion` / `peaks` / `climax_mid` / `suggested_bpm`）
- ⚠️ **两个指标都要**：慢剪辑素材只靠剪辑点会误判（01 实测 82 处切换是真实存在的，
  但早期用 `-v error` 跑 scene detect 会拿到 0 个，从而误判成"单镜头"）
- BPM 三档来源见 SKILL.md §10.1；**`climax_mid` 才是 BGM 该对齐的目标**（不是 `climax`）

### Step 10: 解说声学风格分析（★ 2026-09-28 新增）
```bash
python scripts/analyze_narration.py --video input/source.mp4 --work-dir work
```
- 确定性脚本。输出 `work/narration-style.json`：F0 中位数（判性别）/ 语速（字/秒）/
  解说段能量 → 映射成 `tts_params`（voice / rate / pitch）
- **先过幻觉检测**：判为幻觉时直接给 `has_narration: false` + `tts_needed: false`，
  不要据此生成英文解说（否则是无中生有，SKILL.md §3）
- 若 `tts_needed: true`，Job2 的 TTS 会自动采用这里的 voice/rate/pitch

### Step 11: BGM 选曲 + 实测与高潮对齐（★ 2026-09-28 新增）

**选曲是两层（SKILL.md §12.4），不要一上来就翻本地库：**

> 第一层「选哪首」= **上网搜**（池子是全网的，按视频 BPM/风格选）
> 第二层「从哪儿供给」= **查本地库**（命中 = 缓存，省掉下载+入册）

```bash
# 1) 按上网搜到的候选，逐个查本地库 → 用本地 / 去下载
python scripts/bgm_ledger.py resolve --bpm <rhythm.suggested_bpm> \
    --candidate "网搜第1名::作者" --candidate "网搜第2名" --candidate "..."

# 2) 只有裁决是「需下载」时才下载并入册（曲库 +1）
python scripts/bgm_ledger.py add --audio <下载的文件> --name "<曲名>" --artist <作者> \
    --source <来源> --license "CC BY 3.0" --category high_energy

# 3) 实测 BPM 并生成对齐方案
python scripts/analyze_bgm.py --bgm music/sports/<曲目>.mp3 --work-dir work --track-name "<曲目>"

# 4) 用完记一笔（不记，下次就不知道这首出过场）
python scripts/bgm_ledger.py record --track music/sports/<曲目>.mp3 \
    --video input/source.mp4 --output-dir <产出目录> --profile <profile>
```

- **候选必须来自上网搜索**（用户 2026-09-28 明确要求）：曲库不增长，复用是数学必然。
  `resolve` 的裁决只有两种能继续：
  - ✅ **用本地**（本地有音频**且没用过**）→ **省去下载与入册，直接合成**
  - ⬇️ **需下载**（本地没有音频）→ 下载后 `add`（自动放进 `music/sports/`、
    **实测 BPM** 写入 `bgm-bpm-measured.json`、登记写入 `bgm-library-added.json`
    —— 登记单独放 JSON 是为了不破坏人工 yaml 的注释）
  - ❌ 已用过 / 🚫 许可不行（YouTube Audio Library 等只覆盖 YouTube 站内）/ ❌ 重复候选 → 跳过
- ⚠️ **本地命中必须是"没用过"**：用过就直接复用等于曲库白扩充，又回到"千篇一律"
- ⚠️ **别把 `pick` 当第一步**：`pick` 只看本地库（有库存偏见），只作兜底与搜索线索
- ⚠️ **`*_variant` 不算"换了一首歌"**：母版与变体归同一首（`common.normalize_track_key`）
  —— `music/sports/` 的 34 个文件其实只有 **24 首歌**，"还剩几首可换"按歌曲算
- 确定性脚本。**不采信 `bgm-library.yaml` 手写的 `bpm`**（实测 8/8 偏差 >5%，最差 31%）
- 产出 `work/bgm-plan.json`：实测 BPM / 能量曲线 / drop / `energy_valley` /
  `alignment`（offset 使 drop 落在 `climax_mid`）/ `atempo`（顺带做指纹变形）
- ⚠️ BPM 差得远（>12%）时不要硬拉 atempo（>±6% 观感会变）—— 换曲；`resolve` 会就这点告警

### Step 12: [AGENT] 视频理解
- 使用 `prompts/video_understanding.md`
- 输入：probe 摘要 + 采样帧 + OCR 摘要 + ASR 摘要
- 产出：`work/video-understanding.json`

### Step 13: [AGENT] 文字分类 + Confidence 评估
- 使用 `prompts/classify_text.md`
- 输入：text-tracks.json + ASR transcript + video-understanding.json + profile
- 对每个文字区域：category（9类）+ action（5种：keep/translate/remove/replace/review）+ confidence + reason
- Confidence 三档：≥0.85 自动 / 0.60-0.85 执行+WARNING / <0.60 → action=review
- **translate 必须包含 source_text_removal: true**（移除原中文字幕+背景修复）
- **replace 必须有 replacement.asset**，默认 fit: contain（保持宽高比，禁止拉伸）
- **「只为清理、不产生新文字」的项（含 OCR 误读清理）用 `category: "watermark"`** ——
  它是静态类，既不参与绘制、又会被 SRT 过滤；写成 `other`/`scene_text` 会被**当成字幕写进 SRT**（见 SKILL.md §1）
- 判定线：**≥2 个连续 CJK 且 conf≥0.5** 才算中文残留，单帧单字是噪声（`keep`）。
  但 **OCR 误读也必须清**（QA 用同一引擎抽帧，误读必然复现）—— 清之前先抽「原片 / 成片」
  同一帧裁剪放大确认，再按 **QA 实测的 bbox** 落清理项。
- 产出：分类结果

**OCR 检不出、但你知道该处理的项 → 用 `extra_items_file` 注入（2026-09-27 新增）**
- 计划草稿由脚本起草（SKILL.md §13.3），但**误读框、片头真中文、补画 logo** 这类项 OCR 不会给你。
- 做法：把 items 数组写成 JSON，通过 `extra_items_file` 传给计划构建脚本合并，
  **不要手改脚本**。（本批实现：`tiktok-batch/plan_builder.py --extra-items-file`。）

**长片滚动解说字幕：OCR 只做定位，文本必须人工合并（2026-09-27 05 实录）**
- 判据：OCR 输出里同一行文字在连续帧上**逐渐变长/变短** → 滚动字幕条，走人工合并；
  "整块出现、整块消失" → 普通字幕，OCR 直出即可。
- 做法：① OCR 只取**时间区间**；② 整段听/读一遍合成**完整句子**（05 是 824s → 47 条 cue）；
  ③ 用 `plan_narration.py` 把 cue 转成 `category:"subtitle"` 的 items。
- 这一遍人工合并是**必花的**，放在 Job1 之后立刻做，别等渲染完才发现字幕是碎的（SKILL.md §13.3）。

### Step 14: [AGENT] 音频策略决策（规则化）
- `narration-style.json.has_narration = true`（**且未判幻觉**）→ `audio_strategy = mute_original_tts_bgm`
  ⭐ 此时 Job2 的 TTS 会**自动按原解说的语气语调**生成（读 `narration-style.json.tts_params`
  的 voice/rate/pitch），不需要手填参数。详见 SKILL.md §3。
- `has_narration = false`（**含"ASR 幻觉"判定为 false 的情形**）→
  按用户当前口径（2026-09-28）：**丢掉原片全部音频，只铺新 BGM** → `remove_original_bgm_only`
  ⚠️ 它的实现是**丢掉原片全部音频**（现场声/观众欢呼一起丢）——
  **不是**"只去 BGM、保留人声"（见 SKILL.md §3）。
- **无中文旁白、要 BGM 主导但别丢现场氛围** → `audio_strategy = duck_original_add_bgm`
  ⚠️ 它是"压低"不是"声源分离"：原 BGM 仍可闻，所以**只在无中文旁白时可用**
  （装了 demucs 也做不到"去音乐留观众声"，别承诺，原因见 SKILL.md §3）。
  ⚠️ 且保留原 BGM **不构成版权规避**（音频指纹对增益不敏感），要清版权只有 remove。
- ASR 不可用 → 保守走 `keep_original` + WARNING

**BGM 与节奏（必填进 plan）**：把 Step 11 选定的曲目写进 `audio-plan.json.bgm`，
并保证 `work/bgm-plan.json` 已生成 —— Job2 的混音会读它做对齐（offset/atempo）。

### Step 15: [AGENT] 写 localization-plan.json
- ★ 枢纽契约：Agent 写、Scripts 读
- 包含：profile / review_mode / audio_strategy / bgm / items[]
- ⚠️ **常驻叠字（`title`/`slogan`/`watermark`，窗口覆盖片头）的 `start_time` 必须显式写 `0.0`**：
  渲染会按 `PAD_HEAD`（默认 0.5s）向前扩窗口，若 `start_time > PAD_HEAD`，
  **片头那几帧永远不会被清理**（SKILL.md §4，2026-09-27 真中文残留事故）。
  2026-09-28 起这条已由代码兜底（`common.normalize_persistent_starts`，三处调用），
  触发时打 WARNING —— **看到 WARNING 说明 plan 写错了，回去改 plan，别只依赖兜底**。
- ⚠️ **水印"一族多形"用一条 item 收族，不要按 OCR 变体开 N 条**（SKILL.md §12.5）：
  同一水印在不同帧会被 OCR 吐成不同字、位置也在漂（05 的"热越篮球"= 7 种变体 + ~50 条轨迹）。
  做法：**一条 `category:"watermark"` + `bbox_override`（罩住所有落点）+ `start/end` override（全片）**。
  位置固定的水印仍走精确 bbox，别滥用大框（大框 = inpaint 面积大 = 背景重建变差）。
- 产出：`work/localization-plan.json`

### Step 16: [AGENT] Review 模式（如果 --review 或存在 action=review 项）
- 使用 `prompts/review_report.md`
- 向用户输出报告（编号+时间点+区域描述+建议）
- 等待用户裁决后更新 localization-plan.json
- 产出：`work/review-report.json`

## 异常处理
- 任一脚本退出码 2（致命）→ 停止，上报用户
- 任一脚本退出码 1（可降级）→ 记录 warnings，继续，最终 QA 汇总
- OCR 不可用 → 文字类处理跳过，仅走 ASR 字幕翻译路径
- ASR 不可用 → 保守 keep_original + WARNING
- ⚠️ **本 Job 是长批次最贵的一段（约 8 小时 CPU / 8 条，最长单条 154 分钟）**：
  `asr.json` / `ocr.json` **只在整条跑完时一次性落盘**，中途中断 = 该条全白跑。
  所以**长片要一条一个进程跑并及时确认产物**，别一条命令串完整个目录（SKILL.md §13.10）。
