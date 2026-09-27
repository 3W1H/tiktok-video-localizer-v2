# Job: localize-video

## 目标
根据 localization-plan.json 执行视频清理、翻译、TTS、时间轴对齐、混音、字幕渲染，产出 final.mp4。

## 输入
- `work/localization-plan.json`（枢纽契约）
- `work/probe.json`
- `work/cleaned.mp4`（产出）
- `work/rhythm.json`（Job1 产出；视频节奏，供 §10.1 参考）
- `work/narration-style.json`（★ Job1 产出；TTS 的语气语调来源）
- `work/bgm-plan.json`（★ Job1 产出；BGM 实测 BPM + 高潮对齐方案）
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
> ⚠️ **正式交付走 `scripts/render_overlays.py`**（单 pass：ROI 局部 inpaint + Pillow 精绘英文；
> 见 SKILL.md §12.5.2）。`clean_video.py` 是流水线基底 / 排障路径，它的逐项 drawtext 在窗口
> 重叠时会**叠字**。用 render_overlays 时**不要**再跑本步的字幕绘制。
> 建议直接 `--save-clean-base` 顺手导出**无字底片**，便于后续返工只重绘（§13.4.3）。

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
- ⭐ **语气语调自动对齐原解说（2026-09-28 新增）**：`voice / rate / pitch` 默认从
  `work/narration-style.json` 的 `tts_params` 读（由 Job1 的 `analyze_narration.py` 按原解说的
  F0 / 语速 / 能量算出）。要手工覆盖就显式传 `--voice/--rate/--pitch`。
  实测：同句子 `rate=+15%` 生成 2.160s，`rate=+0%` 生成 2.496s —— 参数确实生效。
- 若 `narration-style.json` 判定 `tts_needed: false`（含 ASR 幻觉判定），
  **不要**再走本步，直接按 `remove_original_bgm_only` 处理（避免凭空造旁白）
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
- ⚠️ **`measure` 只测量、绝不合成音频**（2026-09-27 踩坑）：只调它会让
  `mute_original_tts_bgm` **静默退化成"只有 BGM、没人说话"**（成品听感"正常"，只是没旁白）。
  完整链路必须**先跑 Step 4 的 `audio_processor.py tts`** 再进来测量（SKILL.md §13.4）。

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

### Step 8: 混音（画面用已渲染好的 cleaned.mp4）
```bash
python scripts/audio_processor.py mix --video work/cleaned.mp4 \
    --work-dir work --output-dir output
```
- 按 audio_strategy：
  - `mute_original_tts_bgm`：静音原轨 + 英文 TTS（按 timeline 放置）+ BGM Asset（用户提供或本地曲库）+ 响度归一化
  - `remove_original_bgm_only`：**丢掉原片全部音频**（含现场声）+ 只铺授权纯 BGM
    ⚠️ 名字有误导性 —— 见 SKILL.md §3，它**不是**"只去 BGM 保留人声"
  - `duck_original_add_bgm`（2026-09-28 新增）：**保留原片全部音频但压到 `duck.volume_db`**
    （默认 −15dB）再叠 BGM —— 保住现场欢呼、又让 BGM 主导节奏，集锦片首选。
    ⚠️ 是"压低"不是"去 BGM"（原 BGM 仍可闻），**只在无中文旁白时用**；无 BGM 资产时
    退化为"只有压低后的原声"并给 WARNING（SKILL.md §3）
  - `keep_original`：完整保留原声
- ⚠️ **`--video` 必须显式传 `work/cleaned.mp4`**（返工时传新版；不要让它回落到 work 里的旧成片）
- ⭐ **BGM 自动对齐（2026-09-28 新增）**：混音会读 `work/bgm-plan.json`，由
  `build_bgm_track()` 生成 BGM 轨 —— `atempo`（把 BPM 拉向视频剪辑节奏，兼做 §12.4 的指纹变形）
  → `atrim=start=<offset>`（跳过开头，使 BGM 的 drop 落在视频 `rhythm.climax_mid`）
  → `aloop`（不足则循环，衔接点取能量低谷）→ 淡入淡出。
  没有 `bgm-plan.json` 也能跑（退化为"从第 0 秒平铺"），但**那正是"节奏对不上"的旧行为**。
  验收别只看日志：把生成轨第 3–5s 与原曲第 `offset+3` 秒做归一化互相关，
  实测 NCC = **+0.994**（对照"从头铺"的 −0.056）才算真生效（SKILL.md §10.1）。
- BGM 推荐 ≠ BGM Asset：混入的必须是**在册的本地文件**（`configs/bgm-library.yaml` 人工曲库
  + `configs/bgm-library-added.json` 自动入册）
- **选曲按 `configs/bgm-bpm-measured.json` 的实测 BPM**（登记表 8/8 全错，最差 31%，见 SKILL.md §10.1）；
  选哪首走 `bgm_ledger.py resolve --bpm <suggested_bpm> --candidate "网搜结果..."` ——
  **上网搜决定"选哪首"，本地库只决定"要不要下载"**（本地有且没用过 = 直接合成，省下载）；
  `pick` 只看本地库、有库存偏见，**只作兜底**，别当第一步（SKILL.md §12.4）
- ⭐ **混完音立刻记一笔**（不记下次就会重复用同一首）：
  ```bash
  python scripts/bgm_ledger.py record --track <BGM 文件> --video <源视频> \
      --output-dir <产出目录> --profile <profile>
  ```
  ⚠️ `*_variant` 与母版算**同一首歌**（`normalize_track_key`）—— 台账按歌曲计数，
  所以"换了个 `_variant`"在观众耳里不算换歌（SKILL.md §12.4）
- 无 BGM 时：纯 TTS，post.md 如实写"未混入 BGM"
- 产出：`work/audio-plan.json` + `output/final.mp4`（画面 `-c:v copy` 复用 cleaned.mp4，只换音轨）

### Step 9: 生成字幕文件（**只出 SRT，禁止烧录**）
```bash
python scripts/render_subtitles.py --work-dir work --output-dir output
```
- 从 timeline.json 生成 SRT（不直接用 translated.json）
- ⚠️ **不要加 `--burn`**：英文画面字幕已经由 `render_overlays.py` 精绘进 cleaned.mp4，
  再烧一遍会出现**同一句上下两行**（2026-09-27 实录）。SRT 只是交付物 + QA 校验基准。
- SRT 会自动过滤 `title/watermark/logo/slogan` 等静态项；若某条的 plan 里只有静态项，
  SRT 为 0 字节属**正确行为**（该片的英文是直接画进画面的）。
- ⚠️ **SRT 为 0 字节时，QA 侧也要跳过 `--burn`**：`qa_video.py` 的
  `render_subtitles --burn` 拿空文件喂 ffmpeg 会 `rc=1`，把**整条 QA 链误判为 FAIL**（06 踩到）。
  该类视频的字幕验收改看**画面**（`render_overlays` 的叠字检查），不看 SRT 行数（SKILL.md §13.4）。
- 产出：`output/subtitles-en.srt`

## 异常处理
- clean_video 退出码 1（replace 缺资产）→ Agent 降级该项为 remove 或 review
- TTS 不可用 → 静音原轨 + 字幕 + BGM → WARNING
- timeline unresolved → QA WARNING，不静默丢句
- 清理低置信度区域 → clean-report 标 LOW，QA 抽检残留中文
- ⚠️ **本 Job 的中间件最大**（单条可达 ~2.3 GB：`clean-base.mp4` / `base_XX.mp4` / `seg_XX.mp4` / `masks/`）：
  交付后**立刻删这些大件**（`os.remove` 删文件，`~/Downloads` 下的目录删不掉，见 SKILL.md §13.8），
  只留 `localization-plan.json` / `qa.json` / 四件套 + `work/*.json` 契约。跑批前先看 `df -h`（§13.10）。
