# TikTok Video Localizer

把一条中文视频自动本地化为 TikTok-ready 的英文视频包。

## 功能

- **视频分析**：probe、自适应抽帧（1FPS+场景变化+疑似文字局部加密）、OCR、ASR、文字跟踪
- **智能分类**：5-action 模型（keep/translate/remove/replace/review），Logo 独立于 watermark
- **画面清理**：中文字幕移除+背景修复、水印清除、Logo 替换（contain 保持宽高比，禁止拉伸）
- **音频本地化**：有中文旁白→静音原轨+英文TTS+BGM；无旁白→保留原声
- **时间轴对齐**：原视频时间轴为主约束，TTS 时长为调整依据，支持缩译/拆句/playback_rate 调整
- **字幕渲染**：英文字幕 SRT（**只出 SRT，禁止烧录** —— TikTok 端上传自己的字幕）
- **封面生成**：Profile 驱动，1080×1920，人脸/主体保护，安全文字区
- **发布文案**：九段结构 tiktok-post.md（Summary/Caption+署名/Hashtag/Pinned Comment/BGM推荐/发布时间/Cover/处理结果/文件清单）
- **质量检查**：五类检查 + 残留中文抽检 + 水印复检 + Logo 可见性 + 降级汇总
- **Confidence 三档**：≥0.85 自动 / 0.60-0.85 执行+WARNING / <0.60 进入 review，宁可保留不误删

## 设计原则

- **Agent 决策，Script 执行**：LLM 不做 FFmpeg/OpenCV 能确定性完成的事
- **模型无关**：所有 AI 能力抽象为 Provider，支持 primary → fallback → local → graceful degradation
- **Local First / Lightweight First**：面向普通笔记本，不默认重型模型
- **禁止假装成功**：失败必须记录、降级、上报，QA 如实报告置信度
- **中间产物全部落盘**：`work/*.json` 便于 Debug 与断点续跑

## 安装

```bash
# 必装系统依赖
brew install ffmpeg  # macOS
# 或 apt-get install ffmpeg  # Ubuntu/Debian

# Python 依赖
pip install opencv-python pillow numpy pyyaml

# 本地 OCR（推荐，中文识别）
pip install rapidocr-onnxruntime  # RapidOCR：默认引擎，跨平台、无 native 依赖

# 排障备选（正常流程不需要）
# pip install paddlepaddle paddleocr  # 与 RapidOCR 同源 PP-OCR 模型，仅运行时不同
# pip install pytesseract && brew install tesseract tesseract-lang  # 中文美术字召回≈0

# 可选：本地 ASR
pip install openai-whisper  # Whisper（tiny/base 模型笔记本可跑）
```

## 快速开始

```bash
# 1. 准备输入
mkdir -p input
cp your-video.mp4 input/source.mp4
# 可选：cp your-logo.png input/brand-logo.png
# 可选：cp your-bgm.mp3 input/bgm.mp3

# 2. 运行全流程
python scripts/run_pipeline.py \
    --input input/source.mp4 \
    --work-dir work \
    --profile generic
# ⚠️ 不要传 --output output：那会在视频目录里再套一层 output/ 子目录。
#    缺省时脚本自动推导输出目录（见下），交付物直接与 work/ 同级，不嵌套。

# 3. 查看结果
# 交付物（final.mp4 / cover.jpg / subtitles-en.srt / tiktok-post.md）位于推导出的输出目录：
#   <源视频所在目录>/tiktok/<视频名简写>/
# 与 work/ 同级，不要再套 output/ 子目录。
ls <源视频所在目录>/tiktok/<视频名简写>/
# final.mp4  cover.jpg  subtitles-en.srt  tiktok-post.md
```

### 常用参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--input` | 输入视频路径 | 必需 |
| `--work-dir` | 中间产物目录 | `work` |
| `--output` | 最终输出目录（可不传；缺省推导为 `<源视频目录>/tiktok/<视频名简写>/`，与 `work/` 同级，**不要**传 `output` 去套子目录） | 推导根目录 |
| `--profile` | 风格配置：`nba` / `sports` / `generic` | `generic` |
| `--review` | 低置信度项进入人工复核 | 关闭 |
| `--logo` | 用户英文 Logo 资产路径 | 无 |
| `--bgm` | 用户指定 BGM 路径 | 无 |
| `--resume` | 断点续跑（跳过已完成步骤） | 关闭 |

## 目录结构

```
tiktok-video-localizer/
├── SKILL.md                  # 入口契约：触发、流程、职责边界、异常/QA规则
├── README.md                 # 安装、快速开始、目录说明
├── jobs/                     # 编排层：5 个 job
│   ├── analyze-video.job.md
│   ├── localize-video.job.md
│   ├── generate-cover.job.md
│   ├── generate-post.job.md
│   └── review-report.job.md
├── scripts/                  # 确定性执行层（Python + FFmpeg/OpenCV/Pillow）
│   ├── common.py             # 公共工具：JSON/子进程/退出码/配置加载
│   ├── run_pipeline.py       # 全流程编排入口
│   ├── video_probe.py        # FFprobe 元数据
│   ├── extract_frames.py     # 自适应抽帧（粗扫+局部加密）
│   ├── ocr_video.py          # 本地 OCR（RapidOCR，唯一默认引擎）
│   ├── detect_text_regions.py # 区域聚合去重，产出疑似文字窗口
│   ├── track_text.py         # 跨帧跟踪→start/end/persistence/trajectory
│   ├── generate_masks.py     # 根据 plan 生成时序 mask
│   ├── clean_video.py        # 清理：crop→clone→OpenCV inpaint；Logo replace 贴图
│   ├── tts_timing.py         # TTS 测长→比对→执行调整方案（measure/apply 双子命令）
│   ├── audio_processor.py    # ASR/TTS/混音（asr-only/tts/mix 三模式）+ BGM 节奏对齐
│   ├── analyze_rhythm.py     # 量视频节奏（场景切换+运动强度）→ rhythm.json
│   ├── analyze_bgm.py        # 量 BGM（实测 BPM/能量曲线/drop）→ bgm-plan.json
│   ├── analyze_narration.py  # 量原解说声学风格（F0/语速/能量）→ narration-style.json
│   ├── bgm_ledger.py         # 曲库台账：resolve(选曲) / audit / pick / add / record / seed
│   ├── render_subtitles.py   # SRT 生成（只出 SRT，**禁止烧录**）
│   ├── generate_cover.py     # Profile 驱动封面渲染
│   ├── generate_post.py      # 九段 tiktok-post.md 拼装（含 BGM 署名行）
│   └── qa_video.py           # 五类检查 + 残留中文抽检 + 降级汇总
├── prompts/                  # 智能任务契约（Agent 用自身能力填充）
│   ├── video_understanding.md
│   ├── classify_text.md
│   ├── translate.md
│   ├── tts_adjust.md
│   ├── tiktok_caption.md
│   ├── hashtag.md
│   ├── pinned_comment.md
│   ├── music_recommendation.md
│   ├── cover_copy.md
│   └── review_report.md
├── profiles/                 # 内容风格（纯 YAML，含 cover_style/post_style/bgm_style）
│   ├── nba.yaml
│   ├── sports.yaml
│   └── generic.yaml
├── templates/                # 输出格式模板
│   ├── tiktok-post.md        # 九段结构
│   ├── cover-defaults.json   # 封面引擎默认值（被 profile 覆盖）
│   └── subtitle-style.json   # 字幕样式默认值
├── configs/                  # 全局配置
│   ├── default.yaml          # 采样/confidence/timing/audio/qa 默认值
│   ├── providers.yaml        # AI 能力 fallback 链 + 确定性工具清单
│   ├── bgm-library.yaml      # 人工曲库主干（只读；含授权分层警告）
│   ├── bgm-bpm-measured.json # 实测 BPM 库（登记值 8/8 全错，勿用）
│   ├── bgm-usage.json        # BGM 使用台账（按歌曲计数 → 防复用）
│   └── bgm-library-added.json# 自动入册的新曲目（resolve/add 写入）
├── music/                    # 本地 BGM 曲库（**可增长**：resolve 判需下载时 add 入册）
│   ├── epic/
│   ├── cinematic/
│   ├── sports/               # 当前实际使用；34 文件 = 24 首歌
│   └── motivational/
├── tests/                    # 6 个测试 Case
│   ├── case1_chinese_narration/
│   ├── case2_no_narration/
│   ├── case3_watermark_logo/
│   ├── case4_flash_text/
│   ├── case5_low_confidence/
│   └── case6_provider_fallback/
└── docs/
    ├── architecture.md       # 架构详解 + 数据契约
    ├── pipeline.md           # 完整执行流程
    └── troubleshooting.md    # 常见问题与降级处理
```

## 输出说明

### final.mp4
- 已清理画面中文（字幕移除+背景修复、水印清除、Logo 替换）
- 英文字幕已烧录
- 音频：有中文旁白→英文TTS+BGM；无旁白→保留原声

### cover.jpg
- 1080×1920（9:16）
- Profile 驱动样式（字体/对比度/词数）
- 人脸/主体保护，文字在安全区内

### subtitles-en.srt
- 标准 SRT 格式
- 时间轴以 timeline.json 为准（原视频时间轴主约束）

### tiktok-post.md
九段结构（顺序以 `generate_post.py::render_markdown` 为准）：
1. Video Summary（主题+简介）
2. TikTok Caption（**紧随 CC BY 署名行**，由脚本自动生成）
3. Hashtags（≤5 个）
4. Pinned Comment
5. BGM Recommendations（首选+3备选，标注 Licensing）
6. Posting Time（北京时间 + 美东/美西）
7. Video Cover（文案+设计说明）
8. Processing Report（如实披露降级/低置信度/未解决项）
9. Final Files（四件套清单）

## 脚本退出码约定

| 退出码 | 含义 | Agent 行为 |
|--------|------|-----------|
| 0 | 成功 | 继续下一步 |
| 1 | 可降级失败 | 必须处理：检查 JSON 中的 warnings/errors，决定降级路径或重试 |
| 2 | 致命失败 | 环境/输入问题，直接上报用户，不自动重试 |

## Provider Fallback

每个 AI 能力都有 fallback 链，完全不可用时 graceful degradation：

| 能力 | Fallback 链 | 完全不可用时 |
|------|------------|-------------|
| Vision | Agent Native → Remote → 仅 OCR+ASR 降级理解 | 分类照常，caption/cover 质量下降 → WARNING |
| ASR | Agent Native → Cloud → whisper_local | 保守走 keep_original + WARNING |
| TTS | Agent Native → Cloud → local_tts_cli → skip | 静音原轨+无配音+英文字幕+BGM → WARNING |
| OCR | rapidocr_local（唯一默认，不降级） | 文字类处理全部跳过，仅走 ASR 字幕翻译 → WARNING；此时**不得**声称「成品无中文」 |
| Inpainting | Remote → OpenCV → clone_patch → crop | 清理置信度低标 LOW，残留→WARNING |
| WebSearch | Agent Search → skip | 仅按 profile 选本地 BGM，不虚构 Trending |

任何降级必须写入对应 JSON 的 `provider_used` + `degraded` 字段，`qa.json` 汇总。

## 许可证

MIT
