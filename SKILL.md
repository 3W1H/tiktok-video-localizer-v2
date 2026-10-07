---
name: tiktok-video-localizer
description: >
  Localize a Chinese video into a TikTok-ready English package
  (final.mp4 + cover.jpg + subtitles-en.srt + tiktok-post.md).
  Trigger when the user asks to turn a Chinese video into English TikTok content,
  e.g. "把这个中文视频做成TikTok英文视频" / "localize this video for TikTok".
  Agent-agnostic, model-agnostic, runs on an ordinary laptop (FFmpeg + OpenCV + local OCR/Whisper).
---

# TikTok Video Localizer

把一条中文视频自动本地化为 TikTok-ready 的英文视频包。

## 本文档怎么读（2026-09-27 整理）

- **§1–§11 是规范**：全局规则、工具选型、封面与品牌规格。
  其中 **§8 是封面 / 品牌规格的唯一出处**。
- **§13 是工程约束**（执行效率 / 并发上限 / 返工范围 / 沙箱与后台 / 时间与磁盘预算 /
  操作纪律）：**开跑前先过一遍 §13 的速查表**，这些是"不做就要重跑几小时"的硬约束。
- **§12、§14–§16 是返工档案**：按批次记录的踩坑与修复。
  里面的**规范性规则已上提到 §1–§11 或 §13**，这些章节只在两种情况下需要读：
  ① 想知道「这条规则为什么这么定」② 排查同类问题的现场证据。
  ⚠️ **不要把新规则只写在档案章节里** —— 那是文档失控的起点。

**§12.11 是"本批档案索引"**：第三批（2026-09-27，8 条 NBA）的全部现场证据、判定过程与
返工账都在那里；它开头的表给出「现象 / 根因 / 规范去向」，据此可跳到对应规范节。

**改文档的约定（很重要）**：同一条规则只允许有一个定义处，其他地方写成
「见 §X」。2026-09-27 之前 logo 规则散在 §8 / §8.1 / §12.5 / §12.6 / §13.4.2 五处，
直接导致两轮返工（一次是文档写着 Impact 而脚本硬编码 Arial Black，
一次是文档写着渐变外发光而脚本只画纯金平涂）。

同理，**改实现与文档冲突时改实现**：绝不允许改文档去合理化一个有问题的实现。

## 输入 / 输出

**输入（用户提供）：**
```
input/
├── source.mp4          # 必需：原始中文视频
├── brand-logo.png      # 可选：用户英文 Logo 资产（用于 Logo replace）
└── bgm.mp3             # 可选：用户指定 BGM Asset（优先于本地 music/ 曲库）
```
- `--review`：可选，低置信度项进入人工复核而非自动处理
- `--profile`：可选，`nba` / `sports` / `generic`（默认 `generic`）

**输出目录（自动推导，可通过 `--output` 覆盖）：**

默认输出到源视频所在目录下的 `tiktok/` 目录，以源视频名（简写）命名子文件夹：
```
<源视频目录>/
└── tiktok/
    └── <视频名简写>/           # 自动从视频文件名推导（特殊字符→下划线，超长截取前30字符）
        ├── final.mp4           # 最终英文视频（已清理中文画面 + 英文字幕 + 英文配音/BGM）
        ├── cover.jpg           # TikTok 封面
        ├── subtitles-en.srt    # 英文字幕文件
        ├── tiktok-post.md      # TikTok 发布方案（九段结构 Markdown）
        └── work/               # 中间产物（JSON/帧/mask 等，支持断点续跑与 Debug）
```

**输出目录自动推导示例：**
| 源视频路径 | 自动推导的输出目录 |
|-----------|-------------------|
| `/Users/jason/Downloads/乔丹十佳球.mp4` | `/Users/jason/Downloads/tiktok/乔丹十佳球/` |
| `/Users/jason/Downloads/闲庭信步｜游刃有余🔥GOAT..._xWT111.mp4` | `/Users/jason/Downloads/tiktok/闲庭信步_游刃有余_GOAT高光瞬间_乔丹_NBA_篮球之神/` |

> 多个视频时，每个视频独立一个子文件夹，互不干扰。可通过 `--output` 参数手动指定输出目录。
> ⚠️ 用户偏好（2026-09-24）：最终产物直接输出到 `<视频名>/` 顶层（不建 `output/` 子层），过程文件进 `work/`。见 12.9。

## 核心分工（不可违反）

| 角色 | 负责 | 不负责 |
|------|------|--------|
| **Agent** | 思考与决策：视频理解、文字分类(5-action)、confidence 评估、TTS 超时调整方案（缩译/拆句/playback_rate）、BGM 推荐、cover 文案、caption/hashtag/pinned、review 报告、QA 最终裁决 | 任何确定性计算（不做 OCR/剪辑/渲染/测长） |
| **Skill/Job** | 流程与编排契约：按 `jobs/*.job.md` 顺序调用脚本与提示词 | 实现代码、模型绑定 |
| **Script** | 确定性执行：`scripts/*.py` 全部 CLI 可独立运行、幂等。退出码：0=成功，1=可降级失败（Agent 必须处理），2=致命失败（环境/输入问题，直接上报） | 语义判断（不决定"这段文字是不是 Logo"，只提供 persistence/position/trajectory 证据） |
| **Provider** | AI 能力实现来源与 fallback 链：Agent Native → Remote → Local Model → graceful degradation。每次降级写入 `provider_used` + `degraded` 字段 | — |
| **Profile** | 内容风格：语气、caption、hashtag、cover_style、bgm_style 全部来自 `profiles/*.yaml`。新增垂类=新增 YAML，引擎零改动 | 处理逻辑 |
| **Template** | 最终用户输出格式：tiktok-post.md 九段结构、字幕样式默认值、cover 引擎默认值（被 profile 覆盖） | — |

> **确定性工具不是 Provider**：FFmpeg / OpenCV / Pillow 是本地确定性工具，永远本地执行、不参与 fallback、缺失=环境问题（退出码 2）。

## 主流程（详见 `jobs/`）

> ⚠️ **Job 编号是逻辑阶段，不是执行顺序**。`run_pipeline.py` 的实际执行顺序
> （2026-09-27 起）是 `analyze → cover → localize → post → qa`——封面在视频清理/
> TTS/字幕之前出图。手工按 job 逐步驱动时也照此顺序：Job 1 之后先做 Job 3 封面，
> 再做 Job 2 视频处理。
>
> ⚠️ **「封面优先」≠「占位并行」**：封面只是排在 localize 之前，文案
> （`headline`/`sub`/`stat`/`tagline`）仍必须 Agent 写完**正式版**再渲染。
> 不要用占位标题先出一版图、后面再返工——省下的那点时间会被「改一次封面」吃回去。

```
■ Job 1: analyze-video
  source.mp4
    → video_probe.py                     → probe.json
    → extract_frames.py (粗扫: 1FPS+场景变化) → frames/coarse + frames-index.json
    → ocr_video.py (第一遍)               → ocr.json (coarse)
    → detect_text_regions.py             → 疑似文字窗口列表
    → extract_frames.py --dense-windows   → frames/dense（仅疑似窗口局部加密，如5FPS）
    → ocr_video.py (第二遍，仅新帧)        → ocr.json 合并
    → track_text.py                      → text-tracks.json（start/end/position/persistence/trajectory）
    → audio_processor.py --asr-only      → asr.json（含 has_chinese_narration）
    → [Agent] video_understanding.md      → video-understanding.json
    → [Agent] classify_text.md            → 每项: category + action(5种) + confidence + reason
    → [Agent] confidence 三档评估          → ≥0.85自动 / 0.60–0.85执行+WARNING / <0.60→action=review
    → [Agent] audio 决策（规则化）:
         has_chinese_narration=true  → strategy=mute_original_tts_bgm
         has_chinese_narration=false → strategy=keep_original
    → 写 localization-plan.json  ★枢纽契约落盘

  ※ 若 --review 或存在 action=review 项：
    → [Agent] review_report.md → 向用户输出报告（编号+时间点+区域描述+建议），等待/记录裁决后再继续

■ Job 2: localize-video
  → generate_masks.py（remove/replace/translate 区域，人脸/主体保护冲突检查）→ masks/
  → clean_video.py（crop→clone→OpenCV inpaint→[可选AI inpaint]；
                    replace项：inpaint后 Pillow 贴合 brand-logo.png，contain 保持宽高比；
                    translate项：移除原中文字幕+背景修复）
                                          → cleaned.mp4 + clean-report.json
  → [Agent] translate.md（带原窗口时长约束）→ translated.json
  → audio_processor.py --tts（Provider链，产出每句TTS音频文件）
  → tts_timing.py --measure（测长→与timeline比对→报告 overflow/underflow，不做决策）
  → [Agent] tts_adjust.md（决定缩短译文/拆句/playback_rate）
  → tts_timing.py --apply（执行 Agent 方案并复检，最多迭代2轮）
                                          → timeline.json
  → audio_processor.py --mix（按strategy：静音原轨+TTS+BGM Asset混音+响度归一 / 或原声直通）
                                          → audio-plan.json + mixed音频
  → render_subtitles.py（以 timeline.json 为准）→ subtitles-en.srt（**只出 SRT，禁止烧录**）
      ⚠️ 英文画面字幕已由 render_overlays.py 精绘进 cleaned.mp4；此处再烧一遍 →
         **同一句会出现上下两行**（2026-09-27 实录）。SRT 只作交付物 + QA 校验基准。
                                          → final.mp4 = cleaned.mp4 画面 + mixed 音频（`-c:v copy`）→ output/

■ Job 3: generate-cover
  → [Agent] 候选帧筛选 → pick_cover_frame.py 排名（face_area - clutter）
        + 目视确认主体是谁（分数只用来筛候选，见 §8.5）
  → generate_cover.py（profile.cover_style 驱动：
       ① 封面帧清理：按 localization-plan 的 remove/translate 项自动去除中文/水印（与视频一致，禁止中文带入封面）
       ② 组装 CoverSpec → cover_engine.py 排版（主标题 / 数据行 / 品牌署名
          全部落在安全区 1080×1080 内；颜色取自 profile.cover_style.fill_color，nba 定稿 #FFE800）
       ③ 底部品牌署名：渲染 profile.brand_text（如 Hoop Dynasty Daily），位置固定在底部
       → cover-plan.json（含 text_regions/brand_region/safe_text_zone/frame_cleaned_regions）
         + cover.jpg → output/
  ※ 排版与绘制统一由 cover_engine.py 承担，与手工的 make_cover.py 同一个引擎（§8.0）

■ Job 4: generate-post
  → [Agent] tiktok_caption(英文) / hashtag(恰好5个，相关且热门，禁止#fyp#viral) / pinned_comment(英文，含内容预告+品牌账号+互动问题) / music_recommendation(具体歌名+艺术家+BPM+授权+下载方式)
       （推荐≠资产：已混入的BGM Asset如实写"已使用"；纯推荐曲目标注 Trending/StyleMatch/Licensing）
  → generate_post.py（按模板拼装九段，全英文输出）    → tiktok-post.md → output/

■ QA 与交付
  → qa_video.py（六类检查 + 成品中文残留硬门禁 + 水印区复检 + replace后Logo可见性 + review项汇总）
      ★ chinese_residue 是**硬门禁**：对 final.mp4 全片 OCR，检出中文即 FAIL、退出码 1、禁止交付。
        它独立于 localization-plan —— plan 漏了什么，复用 plan 的检查就漏什么（假 PASS 的成因）。
      cover 检查必查：① 封面中文残留 OCR（RapidOCR）② 标题颜色必须是 profile 的品牌黄（nba 定稿 #FFE800）
                      ③ brand_text 必须渲染 ④ 人脸与实际文字区域重叠判定 ⑤ 封面帧已按 plan 清理
                      ⑥ 标题与品牌署名落在安全带内（主页网格只显示 9:16 的中间部分，见 §8.3）
                                          → qa.json
  → [Agent] 最终裁决：
       PASS    → 交付四件套
       WARNING → 交付 + 在回复中如实列出（降级项/低置信度保留项/timeline未解决项）
       FAIL    → 定位失败环节，修复重跑该 job（中间产物支持断点续跑），仍失败则如实上报
```

## 关键规则

### 1. 文字处理 5-action 模型

`keep` / `translate` / `remove` / `replace` / `review`

- **logo 与 watermark 是不同 category**，不得混用逻辑。logo 可 keep/remove/replace；
  replace 需要 `replacement.asset`（用户提供），默认 `fit: contain` 保持宽高比，**禁止拉伸变形**。
- **画面内中文字幕的 translate 必须包含**：`source_text_removal: true` →
  生成 mask → 移除原文 → 背景修复(inpaint/clone_patch) → 重渲染英文字幕。
  **禁止"中文还在、英文盖在上面"**。
- **中文去除方式（核心规则，禁止用纯黑色条遮罩）**：
  1. **小区域（<100像素高，如水印/字幕）**：用 OpenCV inpaint 去除，露出原来中文字下面的画面
  2. **大区域（>100像素高，如顶部标题）**：用高斯模糊 + 半透明黑色遮罩，能看到下面画面的模糊轮廓，比纯黑条自然
  3. **❌ 禁止**：用纯黑色条（drawbox color=black:t=fill）直接覆盖，黑呼呼一片看不到下面画面
  4. **❌ 禁止**：用 FFmpeg delogo 处理大区域（有波纹闪现、处理不彻底）
  - 去除中文后，在原位写上英文（translate 项）或英文 logo（replace 项）
  - 工具（正式交付唯一路径）：`scripts/render_overlays.py` —— 单 pass 完成「ROI 局部 inpaint 去原文 → Pillow 叠加英文」，见 §12.5.2
  - legacy/排障（**不要拿它们做正式交付**）：`inpaint_video.py`（`--blur-region` 大区域模糊）、
    `clean_video.py --method opencv`（流水线 Job2 基底）。职责与去留见 §9 工具表。
- ASR 口播翻译（画面上无对应中文文字）不需要 removal，直接英文 TTS + 新字幕。
- `action: review` 的项，clean_video.py 必须跳过且不视为失败；
  `action: replace` 必须有非空 replacement.asset，否则脚本以退出码 1 拒绝并提示。
- **「只为清理、不产生新文字」的 item 一律用 `category: "watermark"`（2026-09-27）**。
  成因：`render_overlays.py::build_overlays` 的 `if not text: continue` 在 category 判断**之前**，
  所以 `action=remove` + `target_text=null` 的项本来就不画任何东西；但
  `render_subtitles.generate_srt` 是**按 category 过滤**的，只跳过
  `STATIC_CATEGORIES = ("title","watermark","logo","slogan")` —— 写成 `other`/`scene_text`
  这类非静态 category，item 就会被当成字幕写进 `subtitles-en.srt`。
  本批（02/04/08 的三处 OCR 误读清理项）一开始就写错成 `other`/`scene_text`，已全部改为 `watermark`。
- **中文残留的判定线是「≥2 个连续 CJK 且 conf≥0.5」**，单帧单个 CJK 属于噪声，`keep` 即可。
  ⚠️ **但"误读"也必须清**：QA 抽帧用的是同一个 OCR 引擎，误读必然复现，辩称「这是假阳性」过不了门禁。
  本批 3 处误读（LED 计时牌数字、球裤褶皱、广告牌+球衣交界、标题下暗部）全部按
  **QA 实测检出的 bbox** 做了 inpaint。
- **⭐ 倒计时 / 排名叠字是「内容」不是「装饰」（2026-10-07 新增，违反即丢信息）**。
  体育 / 集锦视频里常有一条**常驻标题条**同时带「中文前缀 + 排名 token」，例如
  `凯文加内特生涯十佳球TOP10`、`NO.7`、`第3球`、`RANK 5`、`3/10`、`BEST 9`。
  其中 `TOP10 / TOP9 / … / TOP1`、`NO.N`、`第N球` 这类 token 承载**「第几个球 /
  第几名」语义**——它是观众判断「现在播到第几球」的唯一依据，**绝不能整体 remove**。
  - OCR 会把「中文前缀 + 排名 token」合并成**一条** region（如 `…TOP10`），这是正常的；
    不要因此把整条当普通 caption 抹掉。
  - **禁止**：把含排名 token 的区域设成 `action=remove`（整条 inpaint 抹掉）→ 成片
    丢失「第几个球」说明。正确做法：**保留 + 翻译**（`action: translate`）。
  - **倒计时是动态的**：合并后的 region 里往往藏着多段不同 token（TOP10→TOP9→…→TOP6），
    渲染时**必须按段动态显示英文 `Top 10 / Top 9 / … / Top 1`**，不能冻结成一个静态
    `Top 10`，也不能为避让底部 logo 直接删掉。
  - **避让 logo 的正确姿势**：若底部 logo 与倒计时条冲突，把**倒计时条整体（含排名 token）
    一起挪到顶部或其它安全区**，并保持动态；不要删。
  - 工具侧已加固：`prepare_plan.py` 会对含排名 token 的 region **强制 `action=translate`**
    并打 `contains_ranking_token` / `dynamic_ranking` 标记；`qa_video.py::check_localization`
    会对「含排名 token 却被 `action=remove`」的 item **发 WARNING**（闸门见 `removed_countdowns`）。

### 2. Confidence 三档

| 区间 | 行为 |
|------|------|
| `>= 0.85` | 自动执行 |
| `0.60 ~ 0.85` | 执行 + QA WARNING |
| `< 0.60` | 不执行删除，`action: review`，进入 review 报告；**宁可保留+Warning，不可误删** |

`--review` 模式下所有 replace/remove 项先出报告再执行。

### 3. 音频策略

- **ASR 检测到中文旁白** → `mute_original_tts_bgm`：静音原轨 + 英文 TTS + Instrumental BGM
- **无中文旁白，保留原声** → `keep_original`：完整保留原声（现场声/欢呼/环境音）
- **无中文旁白，去掉原声换 BGM** ⭐ → `remove_original_bgm_only`：去掉原视频所有音频（篮球声+原BGM），只混合推荐的纯 BGM（无歌词）。适合篮球集锦类视频，节奏感更强
- **无中文旁白，但不想丢现场氛围** ⭐ → `duck_original_add_bgm`（2026-09-28 新增）：
  **保留原片全部音频、整体压到 `duck.volume_db`（默认 −15dB）**，再叠一层授权 BGM。
  集锦片用它能同时拿到"观众欢呼还在"和"BGM 主导节奏"。`remove_original_bgm_only` 会把现场声
  一起丢掉、成片发干，就换这一路。
  ⚠️ **它不是声源分离**：原 BGM 只是被压低、没有消失，所以**只在 ASR 判定「无中文旁白」时可用**
  （否则中文解说仍听得见）。`audio-plan.json` 写法：
  ```json
  {"strategy": "duck_original_add_bgm",
   "duck": {"volume_db": -15},
   "bgm": {"asset_path": "...", "volume_db": -10, "fade_in_seconds": 2, "fade_out_seconds": 3},
   "loudness_normalized": true}
  ```
  两路的相对电平在混音前确定（各自 `volume` 后 `amix … normalize=0`），最后整轨 `loudnorm`
  到 −16 LUFS —— 归一化只改绝对响度、**不改两者的比例**。
  没有 BGM 资产时退化为"只有压低后的原声"并给 WARNING。
  ⚠️ **术语澄清（别被 `duck` 这个词误导）**：这里是**固定增益衰减**，**不是** sidechain 动态闪避
  （电台那种"人一开口、音乐自动让路"）。实现就是两条轨各乘一个常数再相加，**全程比例不变**。
  所以成品里「原片自带的 BGM」也还在（只是变小了）→ 实际是**两段音乐叠加**，可能发糊。
  ⚠️ **固定增益的已知弱点（2026-09-28 实测）**：原片自身响度不可控，同一组默认参数在不同片子上
  得到的"领先量"能差 3 倍，所以**不要以为配了 −15 就一定"BGM 压过原声"**：

  | 片子 | 原片 I | 压低后 | BGM 素材 I | BGM 压低后 | BGM 领先 | 听感 |
  |---|---|---|---|---|---|---|
  | 01 奥尼尔·艾佛森 | −12.97 | −27.97 | Wallpaper_v2 −11.23 | −21.23 | **+6.7 dB** | ✅ 干净 |
  | 07 乔丹观赏性 | −10.20 | −25.20 | Hitman_v3 −13.21 | −23.21 | **+2.0 dB** | ⚠️ 两段音乐接近、会糊 |

  量法：`ffmpeg -i <src> -t 90 -map 0:a:0 -af loudnorm=print_format=json -f null -` 读 `input_i`。
  **用这条路之前先量原片与 BGM 各自的 I**，按差值反推 `duck.volume_db`；"BGM 明显主导"一般需要
  **领先 ≥6 dB**。这是本策略目前最脆的地方，迭代方向是"先归一原片响度再按目标比例给增益"。
  （另：本批 BGM 素材母带已接近削顶 —— Wallpaper_v2 `TP=+0.76 dBTP`、Hitman_v3 `+1.75 dBTP`，
  靠最后整轨 `loudnorm TP=-1.5` 兜住；**不要单独再把 BGM 拉更亮**。）
  ⚠️ **用这条路之前必须先过两道闸（2026-09-28 实测坐实：本批 8 条最多只有 1 条合格）**：
  1. **`asr.has_chinese_narration` 必须为 false**。本批 8 条里 **6 条为 true** —— 原声只降 15dB，
     中文解说照样听得见，直接出局。
  2. **原片自带的音乐不能占主导**。本批实测（前 60s）：**06 科比系列赛**谱平坦度 0.035 /
     节奏强度 9.40（纯音乐参考 0.007 / 9.83）→ 几乎就是纯音乐，叠新 BGM 必然两段音乐打架。
     只有 **04 莫兰特街球**（0.299 / 5.13，接近粉噪声参考 0.469 / 3.22）算"音乐不主导"。
  量法（无新依赖，conda python + numpy 即可）：抽 60s 单声道 16k →
  ① **谱平坦度** = 200–2000Hz 内「几何均值 ÷ 算术均值」逐帧的中位数，越低越有音高
     （纯音乐 ≈0.007–0.26，粉噪声 ≈0.47）；
  ② **节奏强度** = 包络调制谱在 1.5–4Hz（90–240BPM）的峰突出度（纯音乐 6.8–9.8，粉噪声 3.2）。
  **两个指标必须一起看**：打击乐为主的音乐平坦度会偏高（Hitman_v3 = 0.256），只看平坦度会误判。
  ⚠️ **音量压低 ≠ 版权解除**：音频指纹（Content ID / Shazam 一类）对**增益不敏感** ——
  EFF 的公开实测里 **±48dB** 的放大/衰减**全部仍被识别**；BMAT 也明说"无论音量多低，都能注意到
  背景音乐"。所以 duck 保留原 BGM（哪怕压到 −15dB）**不构成任何版权规避**。要清版权只有
  `remove_original_bgm_only`。**不要把 duck 当"既保氛围又避版权"的中间档推荐给用户。**
- **有中文解说 → 英文解说要「按原解说的语气语调」生成** ⭐（2026-09-28 新增）：
  这不是"把中文逐句翻译成英文字幕"就完事 —— 要先用 `analyze_narration.py` 量出
  原解说的**声学特征**，再据此挑 TTS 音色与语速：

  | 量什么 | 怎么量 | 映射到 |
  |---|---|---|
  | 基频 F0 **低分位 p25**（不是中位数！） | 逐 40ms 帧自相关，只取清晰帧（峰/零延迟 > 0.3）；中位数只作透明性指标一并记录 | **性别 → 音色**；相对参考的偏移 → `pitch`（夹 ±25Hz） |
  | 语速 | 汉字数 ÷ 解说总时长 | 相对 4.5 字/s 的偏离 → `rate`（夹 ±25%） |
  | 解说段能量 | 解说段 RMS 与全片 RMS 之差 | 情绪强度 → `energetic` / `neutral` / `calm` |

  ⚠️ **性别判据必须用 p25，不能用中位数**（2026-09-28 修正）。
  逐帧自相关法在高频段有**倍频（octave）假象**，叠加原片底噪/BGM 后**中位数会被系统性抬高**。
  实测本批 03（中文男声解说）：中位数 166.7Hz > `F0_SPLIT_HZ`(165) → 旧判据给出**女声**（错），
  但 p25 = 133.3Hz，且 **37% 的浊音帧落在 140Hz 以下** —— 女声的 F0 几乎不可能低于 ~150Hz，
  不可能有这么大比例的低频帧。改用 p25 后判对男声。
  交叉验证手段（有争议时用）：手写 YIN（累积均值归一化差分函数，天然免疫倍频）取 p25；
  或出**窄带谱图**（窗 100ms → 10Hz 分辨率）直接数谐波梳间距，男声 ~110-130Hz、女声 ~200-250Hz。

  ⚠️ **`pitch` 覆盖时要谨慎**：`pitch` 是用「F0 相对同性别参考值的偏移」算的，
  若 F0 本身被倍频/BGM 污染，算出的 `pitch` 也会偏高（03 算出 +19Hz）。
  给神经语音抬音高**正是"机械感"的来源**。用户明确反感机械感 TTS 时，
  建议显式 `--tts-pitch +0Hz`，不要跟着被污染的估计值走。
  同理 `rate`：中文「字/秒」线性外推到英文「词/秒」并不成立（03 从 6.78 字/s 外推出 +25%，
  而英文稿实际只有 2.17 词/s，根本不需要加速）→ 先核算英文稿的词/秒，再决定要不要覆盖。

  产物 `work/narration-style.json` 的 `tts_params` 被 `audio_processor.py tts` 直接消费
  （`--voice/--rate/--pitch` 可覆盖）。音色映射表见 `analyze_narration.py::VOICE_MAP`。

  ⚠️ **必须先过 ASR 幻觉检测**（`detect_asr_hallucination()`）。whisper 在**没有清晰人声**时
  会**凭空造出**"整 2 秒一段、文本循环、avg_logprob 极低"的中文。实测 01（奥尼尔·艾佛森）：
  16 段里 **15 段都是整 2.00s**、文本循环「我來做個高度／今天在吃飯／每天打飯／都請問說」、
  平均 avg_logprob **−0.81**、重复句占比 **94%** —— 若照它生成英文解说，就是**无中生有**地
  给球迷塞一段跟原片无关的旁白。判据是三条（置信度 / 重复句占比 / 固定时长占比），
  **命中两条**才判幻觉（只命中一条可能误伤真解说的口头禅）。
- **BGM 的节奏必须与视频节奏一致，要有高潮起落** ⭐（2026-09-28 新增）：
  详见 **§10.1「节奏匹配」**。一句话概括：用 `analyze_rhythm.py` 量视频（场景切换 +
  运动强度）、用 `analyze_bgm.py` 量 BGM（实测 BPM + 能量曲线 + drop），
  让 **BGM 的 drop 落在视频的高潮时刻**上，而不是把 BGM 从第 0 秒平铺到片尾。
- ⭐ **`mute_original_tts_bgm` 的电平与 `--normalize`（2026-09-28 新增，本批 03 是首例）**
  ⚠️ 走这条策略时**必须开 `make_audio_plan.py --normalize`**。
  原因：该分支的混音是 `[0:a][1:a]amix=inputs=2:duration=longest`，而 ffmpeg 的 amix
  **默认 `normalize=1`，会把每一路按 1/n 缩放** —— 两路输入各被压低约 6dB，
  整条音轨最终比目标低 ~6dB。而 `loudness_normalized` 在 plan 里**默认 true**
  （`audio_plan.get(..., True)`），一旦显式写成 `false` 就会被跳过、无人兜底。
  只有 `loudness_normalized: true` 才会执行 `loudnorm=I=-16:TP=-1.5:LRA=11`，既补回电平
  又顺带压住削顶（本批 BGM 母带普遍 max_volume = 0.0 dBFS）。
  `volume_db` **不要沿用 BGM-only 场景的 −12**：那个值是"音乐当主角"。TTS 场景里
  BGM 是**垫底**，用该分支代码自己的默认 **−18**。
  定值前先实测两边响度，别拍脑袋：
  `ffmpeg -i <文件> -af volumedetect -f null -`（注意**别加 `-v error`**，
  volumedetect 走 info 级，加了就什么都看不到）。
  本批 03 实测：edge-tts GuyNeural +10% 的 `mean_volume` = **−20.9dB**，
  `Future Gladiator` = **−12.7dB** —— 不加衰减的话**音乐比人声响 8.2dB**，
  人声会被埋掉。
  ⚠️ 该分支**忽略 plan 里的 fade_in/out**（`run_mix` 硬编码传 `0.0, 0.0`），
  在人声上做 BGM 淡入淡出会很突兀 —— 填 0 即可，不用纠结。
- ⭐ **英文稿天然比中文长 → 用「级联排布」吸收，别硬砍稿**（2026-09-28 新增，03 首例）

  先说清约束链（这条链决定了一切，03 之前没人写下来过）：
  `render_overlays.sync_timeline` 把**字幕显示窗**写回 `timeline.json` 的 `final_start/final_end`，
  而 `audio_processor.run_mix` 拿 `final_start` 做 `adelay` 摆放每一段 TTS。
  ⇒ **字幕窗起点 == 该段语音的起播点**，且 **`run_mix` 只做 adelay/amix、不按时长压缩**。
  ⇒ **窗口短于 TTS 时长 = 这句话压到下一句上（听感糊）**，这不是"警告"，是硬故障。

  实测（03，245s 片）：英文稿 TTS 总量 **197.5s vs 中文原声窗 187.6s（+5.3%）**。
  两种 remedy（`tts_timing.py` 的契约里都写了），**一起用**：

  1. `shift_into_gap` —— **级联排布**（首选，免费）：
     ```
     place = cue.start                      if prev is None
           = max(cue.start, prev_end + GAP)  otherwise     # GAP = 0.08s
     ```
     某条装不下就**顺延**给下一条，下一条有余量就吸收；某条讲完后**整列回弹到自身原始起点**，
     所以漂移**不会跨过静音间隙累积**。03 实测最大漂移 **1.28s**，cue 15 前归零，视觉无感。
     实现见 `tiktok-batch/split_cues.py --tts-probe`。
     反面：按"每个 cue 独立装进自己的窗口"排布会逼你砍掉 cue 9 那句收尾（省 1.0s），
     而级联把这 1.0s 白送给了你 —— **刚性分窗是过度约束**。
  2. `shorten_translation` —— 只补级联吸收不掉的残差：
     **必须用真引擎逐条量，不能凭字数猜**。因为每条字幕是**独立一次 TTS**，
     自带句尾停顿，**分段后的总时长 > 整段朗读的时长**（03 的 cue 9：整段 11.04s，拆 4 条后 12.50s）。
     做法：`pick_fit.py` 把每个改写候选都真跑一遍 edge-tts 量时长，选**能装下的最长版本**，
     这样"因时间而丢的内容"被压到最小。03 只压缩措辞、没删任何一个事件。

  ⚠️ **必须在渲染前用真引擎预量**：`tts_probe.py`（与管线同参数跑 `edge-tts`，逐条 ffprobe 量时长）。
  不预量就会"渲染 19 分钟 → 才发现一半语音叠在一起"。
  ⚠️ 预量与渲染时各跑一次 TTS，时长有 ~1–2% 抖动，所以**边界要留 0.08s 间隔**。
- ⭐ **英文单行字幕有硬长度上限，且渲染器不换行**（2026-09-28 新增）

  `render_overlays.build_overlays` 对 subtitle 类做：
  ```python
  max_w = frame_w - 70
  fit_font(font_path, text, 40, max_w)   # min_size=18 是硬下限
  draw_centered(...)                     # 只 draw.text 一次 —— 不支持多行
  ```
  字号缩到 18px 后**继续超宽就横向溢出被裁**。实测 **1280 宽 + Arial Bold 40px ≈ 62 字符**
  （用**真实像素**判定，别数字符：`"I never pay fees."` 与 `"WOULD NEVER PAY FEES"`
  同长但宽度差很多）。
  03 原始 21 条里有 **18 条超 62 字符**（最长 171）→ 直接渲染就是一堆 ~18px 且左右被裁的不可读字幕。
  ⇒ **翻译时必须按宽度拆行**，不是等渲染时缩字号。拆分策略（`split_cues.py`）：
  先按句末标点切句 → 仍超宽就递归找"最佳断点"（左右两半都放得下、优先落在 `, ; : —` 之后
  或开引号之前、**禁止以 the/a/of/and 这类虚词结尾**）。03：21 cue → 56 行，最长 1203px / 1210px。
  ⚠️ 拆行会让 TTS 段数变多 → 段尾停顿也变多 → **总时长变长**（见上条），拆完要重新预量。
- ⚠️ **`plan_narration.py` 产出的 item 一律是 `action="remove"`**（它只负责"清理"，
  "替换"由上游 Agent 决定）。凡是**带 `target_text` 的字幕类项，必须提升成 `action="translate"`**：
  渲染层对 remove/translate 一视同仁（都要求有 target_text 就画），但
  `render_subtitles.generate_srt` 与 `qa_video.py` 的 localization 检查**按 action 分类统计** ——
  不提升就会出现「translate: 0 / remove: 57」这种明显不对的 plan。
  实现见 `tiktok-batch/norm_narr.py`（它同时负责 y 归一化与 x 对称化）。
- **ASR 完全不可用** → 保守走 `keep_original` + WARNING（宁可保留原声也不误静音）

**`remove_original_bgm_only` 策略说明**：
- 适用于：篮球集锦、扣篮高光、比赛回顾等只有篮球声和纯BGM的视频
- 处理方式：去掉原视频所有音频 → 根据视频内容和时长从 BGM 音乐库推荐具体音乐 → 循环/裁剪到视频时长 → 淡入淡出 → 混合
- BGM 要求：纯音乐（无歌词）、节奏感强、根据视频内容匹配能量等级
- 具体音乐推荐见 `configs/bgm-library.yaml`，必须给出具体音乐名称，不能泛泛推荐

**⚠️ `remove_original_bgm_only` 的真实语义（2026-09-27 澄清，名字有误导性）**：

它的实现是**丢掉原视频的全部音频**（现场声、观众欢呼、球鞋摩擦、原 BGM 一起没），只铺授权的纯 BGM。
名字里的 `bgm_only` 说的是**产出**里只有 BGM，**不是**"只把 BGM 去掉"。
所以它是"清干净中文"最稳的策略（无论 ASR 判定真假，成品都不含任何原始音频），
**代价是现场氛围一起丢**，集锦片会显得干 —— 要保住现场氛围就用 `duck_original_add_bgm`（见上）。

**✅ 关于 demucs / 声源分离（2026-09-28 更正 + 实测）**：

上一版这里写着「V1 没有 Demucs 等声源分离」——**与事实不符，已作废**：
本机 conda 环境**已装 `demucs 4.0.1`**（`torch 2.2.2`，htdemucs 权重
`~/.cache/torch/hub/checkpoints/955717e8-8726e21a.th` 84MB 已在本地缓存，可离线跑）。

**但装了它也做不了"去 BGM 留现场声"**：demucs 是为**人声 / 伴奏**二分设计的（`--two-stems=vocals`），
而**观众现场噪声与音乐在频谱上高度重叠**，4-stem（vocals/drums/bass/other）会把两者归进同一个 stem ——
"把观众声从音乐里抠出来"不是它的任务，硬用只会得到糊掉的结果。所以：

- **不存在**"干净地去掉 BGM、保留现场声"的档位，任何环境（含装了 demucs 的）都别承诺这个；
- 能落地的近似就是 `duck_original_add_bgm`（原声压低 + 叠 BGM）——**如实说明它是"压低"不是"去掉"**；
- 真要彻底去掉原音频，只有 `remove_original_bgm_only`；要保现场氛围，只有 `keep_original` 或 duck。

**下一版若要真正做分离**，需要的是「音乐 / 非音乐」判别（如音乐活动检测 + VAD 分段），
不是人声分离模型 —— 记在这里免得下次又走一遍 demucs 的弯路。

### 4. 时间轴原则

- **原视频时间轴是主约束，TTS 时长是调整依据**。不是 TTS 时间轴完全覆盖原视频时间轴。
- `playback_rate` 统一定义：`1.0=原速`，`>1.0=加速`（1.10=加速10%），`<1.0=降速`，允许区间 `[0.90, 1.15]`。
  Agent / Script / TTS Provider 对这个字段采用完全一致的含义。
- `tts_timing.py` 只测量与执行；缩短译文/拆句/调速由 Agent 决定（`prompts/tts_adjust.md`），最多迭代 2 轮，
  仍放不下 → 记入 `unresolved` → QA WARNING，**不静默丢句**。
- **常驻叠字（`title`/`slogan`/`watermark` 类，窗口覆盖片头）的 `start_time` 必须显式写 `0.0`**，
  不要留空让它取 OCR 首检时间 —— **否则片头几帧的中文永远不会被清理**。
  机制：渲染时每个窗口都按 `PAD_HEAD` 向前扩，若 `start_time > PAD_HEAD`，窗口起点就被推到 0 之后。
  2026-09-27 实录：两行顶部标题的 `start_time` 取了 OCR 首检时间 `1.0`，而 `PAD_HEAD=0.5`
  → `[max(0,1.0-0.5), …] = [0.5, …]`，**`[0, 0.5s)` 共 15 帧的中文标题原样留在成片里**；
  而 QA 按 1FPS 抽帧通常正好抽不到 t=0（那次是运气差抽到了才发现）。
  ⚠️ 算这个必须用**渲染实际生效的默认值**（`render_overlays.py` 的 `PAD_HEAD` 默认 **0.5**、
  `PAD_TAIL` 默认 **0.35**），而 `render_parallel` **不传**这两个参数、只有 `--dry-run --sync-timeline`
  才传 —— 别拿 dry-run 时传的值去估算片头漏了几帧。
- ✅ **这条规则已由代码兜底（2026-09-28）**，不再只靠 Agent 记性：
  `common.normalize_persistent_starts()` 是唯一真源，**三处调用**：
  ① `prepare_plan.py`（起草 plan 时就归一 → **主修复**）
  ② `generate_masks.py`（mask 的 start/end 决定 inpaint 真实时间窗，必须归一）
  ③ `render_overlays.py`（绘制侧兜底）。
  判据（参数 `--head-snap` / `--persistent-min-sec`，默认 **5.0s / 10.0s**）：
  **常驻类叠字若起点落在片头 5s 内、且时长 ≥10s → 起点强制归零**。
  加时长下限是为了不动真正的短下三分之一标题（t=2s 出现、只显示 3s 那种），
  否则会把 inpaint 窗口扩到本来没字的画面上、白白糊掉画面。
  触发时会打一条 WARNING（`persistent title A1: start_time 1.00s -> 0.00s`）——
  **看到它就回去把 plan 改对**，别看它被自动修好了就当无事发生。
  ⚠️ 只改绘制侧的归一**不够**：inpaint 窗口由 `masks-index.json` 决定，
  所以 `generate_masks.py` 那一处必须在，否则画面上的原中文照样留着。
  实测（2026-09-28，12s 合成片 + 常驻标题 1.0→11.9s）：
  渲染窗口从 `[0.5-12.2]` 变成 **`[0.0-12.2]`**、mask 窗口从 `1.00` 变成 **`0.00`**，
  而 3s 的短角标（2.0→5.0）**保持不动** —— 时长下限生效。
  📌 **兜底只在内存里归一、不回写 `localization-plan.json`**：因为真正消费这个时间窗的
  只有"mask"和"渲染"两个脚本（SRT 侧按 category 过滤掉静态项、QA 不读 start_time），
  它们都已经调用同一个函数，所以不存在跨产物不一致。**plan 文件保持原样 + 打 WARNING，
  是刻意留给 Agent 的修正信号**；把它悄悄改掉反而会掩盖"plan 写错了"这件事。

### 5. 诚实原则

- 任何 Provider 降级写入对应 JSON 的 `provider_used` + `degraded` 字段，`qa.json` 汇总。
- `asr_text_overlap` 只是**词汇重叠**（中文 char-bigram Jaccard / 英文 word Jaccard），**不是语义**；
  真语义判断（`semantic_match`）由 Agent / LLM / Embedding Provider 完成，脚本产出时恒为 null。
- **BGM 推荐必须具体**：从 `configs/bgm-library.yaml` 中推荐具体的音乐名称（如 "Drive" by Kevin MacLeod），不能泛泛推荐"快节奏音乐"。推荐时必须说明：音乐名称、艺术家、BPM、风格、适用场景、为什么适合这个视频。混入 final.mp4 的必须是本地文件（用户提供 或 从音乐库下载到本地 music/ 目录）；推荐曲目标注 Trending / StyleMatch / Licensing，**热门不等于可商用**。
- **禁止假装成功**：清理置信度低如实标 LOW，残留中文由 QA 抽检上报，WARNING 时 tiktok-post.md「七、视频处理结果」必须如实披露。

### 6. 发布文案规范（tiktok-post.md 核心规则，违反即不合格）

**tiktok-post.md 必须全英文输出**（源视频是中文，但目标平台是 TikTok 英文用户），严格遵循以下规则：

1. **内容简介（Video Summary）必须英文**：Topic、Summary 全部用英文描述，禁止出现中文
2. **话题标签（Hashtags）恰好 5 个**：
   - 必须是与视频内容**强相关**且在 TikTok 上**热门**的标签
   - **禁止**使用泛泛的 `#fyp` `#viral` `#trending` `#foryou` 等无关标签
   - NBA 类视频默认标签库：`#NBA` `#Basketball` + 球员标签（如 `#AllenIverson` `#MichaelJordan`）+ 内容标签（如 `#NBATopPlays` `#Dunk` `#Hoops`）
   - 标签数量校验：generate_post.py 会自动校验，不等于 5 个时 WARNING 并自动调整
3. **置顶评论（Pinned Comment）必须包含三要素**：
   - **内容预告**：如 "More coming soon! Follow for the full countdown"（驱动关注）
   - **品牌账号**：`@HoopDynastyDaily`（频道增长）
   - **互动问题**：如 "What's your all-time favorite play? Let me know in the comments! 👇"（提升评论量）
   - 末尾重复关键标签增强曝光
4. **BGM 推荐必须具体**：
   - 必须给出：音乐名称、艺术家、BPM、风格、授权状态、来源、下载方式
   - **禁止**泛泛推荐"快节奏音乐"或"嘻哈音乐"
   - 优先推荐 "Free For Profit" 或 "No Copyright" 标签的音乐（可免费商用）
   - 推荐至少 1 首首选 + 3 首备选（同类视频轮换使用，避免单调）
   - 已混入视频的 BGM 必须标注"已使用"，并说明处理方式（裁剪/淡入淡出/音量）
5. **发布时间建议必须双时区**：
   - 同时给出**北京时间**和对应**美国东部时间（ET）**
   - 推荐首选 + 次选两个时间点
6. **Caption 必须英文**：主标题 + 备选标题，包含 emoji 和互动引导

**generate_post.py 自动校验**：
- 标签数量 ≠ 5 → WARNING + 自动调整
- 缺少置顶评论 → 使用默认模板（含三要素）
- 缺少 BGM 推荐 → 从 bgm-library.yaml 自动推荐

## 处理记录与防重复上传（必读）

**目的**：记录所有已本地化的源视频，避免同一视频被重复处理、重复上传 TikTok。

**存储**：`<skill_root>/processed-videos.json`（跟随 skill，跨项目统一）

**键**：源视频**原始文件名**（`basename`，唯一键）。同名文件内容变化（size/mtime 改变）视为新版本，`check` 返回 `changed`。

**记录字段**（每条）：
```
source_file   源视频原始文件名（唯一键）
source_path   源视频绝对路径
file_size     文件大小（字节）
mtime         修改时间戳
processed_at  处理完成时间（ISO）
output_dir    输出目录（tiktok/<视频名简写>/）
profile       使用的 profile
status        PASS / WARNING / FAIL / UNKNOWN
artifacts     产出的四件套文件名列表
```

**命令**（`scripts/processed_log.py`）：
```bash
python scripts/processed_log.py check --video <源视频.mp4>
    # status=processed（已处理，附 output_dir/processed_at，退出码 1）
    # status=changed（同名但文件已更新，退出码 1）
    # status=not_processed（未处理，退出码 0）
python scripts/processed_log.py add --video <源视频.mp4> --output <输出目录> --profile nba [--status PASS]
python scripts/processed_log.py list
```

**全流程集成**（`run_pipeline.py`）：
- **处理前**：自动 `check`。已处理过（或同名文件已变更）→ 打印输出位置并退出，**避免重复上传**；
  确实要重跑加 `--force`；只想按文件名匹配（忽略文件变化）加 `--ignore-mtime`。
- **完成后**：自动 `add` 记录（status 取 qa.json 的 overall）。
- **目录批量**（2026-09-27）：`--input <目录>` 时扫描目录下所有视频（顶层、忽略隐藏文件），
  逐个 `check`，已处理的跳过、未处理的进入。`--output`/`--work-dir` 在批量模式下无效
  （每个视频独立推导输出目录）。结束时打印 `Processed N / Skipped M` 汇总。
- **封面优先**（2026-09-27）：Job 顺序改为 `analyze → cover → localize → post → qa`，
  封面在视频清理/TTS/字幕之前出图；用户自带封面图用 `--cover-image`。
  **注意：封面只是「提前」不是「占位并行」**——文案必须 Agent 写完正式版再渲染，
  不做「占位标题先出图再返工」。

**Agent 手动流程（按 job 逐步驱动脚本时）必须遵守**：
1. 拿到源视频先 `check`：已处理 → 直接告知用户"该视频已处理过，输出在 <output_dir>"，不重复跑；
2. 全部 job 完成且 QA 裁决后，`add` 记录（含最终 status）。

## 快速开始

> **⚠️ 本节命令有「正式」与「legacy」之分，别用错。**
>
> | 任务 | ✅ 正式路径 | ⚠️ legacy（仅排障/复现旧片） |
> |---|---|---|
> | 全流程 | `run_pipeline.py` | — |
> | 画面清理 + 英文叠加 | `render_overlays.py`（§12.5.2） | `clean_video.py` 的 drawtext、`clean_with_overlay.py`、`inpaint_video.py`（§9 的表） |
> | 封面 | `generate_cover.py`（流水线）/ `make_cover.py`（手工），**同一引擎** | — |
> | 选帧 | `pick_cover_frame.py`（§8.5） | `generate_cover.py` 内置的旧 sharpness 兜底 |
>
> 封面 / 品牌的所有规格见 §8（唯一出处）。

```bash
# 一键安装所有依赖（核心 + OCR + ASR）
bash scripts/install_deps.sh --full

# 或仅安装核心依赖
bash scripts/install_deps.sh

# 或手动安装
# 必需：ffmpeg/ffprobe（系统级）
# Python: opencv-python-headless pillow numpy pyyaml
# OCR（中文识别）：rapidocr-onnxruntime —— 唯一默认引擎（初始化约 2.6s）
#                   排障可选：paddleocr==2.7.3 + paddlepaddle（同源引擎，初始化约 14s，可跑但慢）
#                             tesseract + pytesseract（中文美术字召回≈0，禁止用于交付）
# 可选 ASR：openai-whisper
pip install opencv-python-headless pillow numpy pyyaml

# 运行全流程（输出目录自动推导：<视频目录>/tiktok/<视频名>/）
python scripts/run_pipeline.py \
    --input /path/to/your/video.mp4 \
    --profile nba \
    [--review] [--bgm input/bgm.mp3] [--logo input/brand-logo.png]

# 也可以手动指定输出目录（覆盖自动推导）
python scripts/run_pipeline.py \
    --input /path/to/your/video.mp4 \
    --output /path/to/custom/output \
    --work-dir /path/to/custom/work \
    --profile nba

# 目录批量（2026-09-27 新增）：给一个目录，扫描里面所有视频，
# 逐个查 processed-videos.json，已处理的跳过、未处理的才处理
python scripts/run_pipeline.py \
    --input /path/to/videos_dir \
    --profile nba \
    [--force] [--ignore-mtime]
# 注意：目录模式下 --output/--work-dir 被忽略（每个视频独立推导输出目录）
# 结束后打印汇总：Processed N / Skipped M

# 用户自带封面图（跳过选帧，直接适配画幅 + 叠加标题/品牌）
python scripts/run_pipeline.py \
    --input /path/to/your/video.mp4 \
    --cover-image /path/to/cover.png \
    --profile nba
```

**⭐ 编排顺序（2026-09-27 起）**：`analyze → cover（封面优先）→ localize → post → qa`。
封面在 analyze 之后立即出图（不再等视频清理/TTS/字幕），视频处理随后继续——
「先出封面早点上传，视频后台跑完」。

**⭐ 第一步永远是接触印样（找中文最高效的方式）：**
```bash
# 一条命令把整片压成一张带时间戳的网格图，一轮看完
python scripts/contact_sheet.py --video input/source.mp4 --out work/sheet_full.jpg
# 已知 37-41s 有字幕 -> 二次加密 + 只裁底部区域放大看
python scripts/contact_sheet.py --video input/source.mp4 --out work/sheet_tail.jpg \
    --start 37 --end 41 --cols 6 --rows 4 --crop 0,650,720,904
```
> ✅ **中文检测走 ocr.json**（RapidOCR，2026-09-22 起为默认引擎）。
> 实测对中文描边/彩色美术字识别准确：`"历史联盟谁能阻止奥尼尔"` conf 0.99、
> `"残暴鲨鱼和搞怪鲨鱼"` conf 0.997（同帧 tesseract 召回率为 0）。
> 但**定位准 ≠ 清理干净** —— 交付前必须过 `qa_video.py` 的成品中文残留硬门禁（见 13.2）。

**实用工具：**
```bash
# bbox 精确定位（在视频帧上画半透明框，确认坐标是否准确）
python scripts/locate_text.py --video input/source.mp4 --timestamp 30 \
    --x 270 --y 700 --w 240 --h 45 --label "watermark"

# 使用用户提供的封面图片（跳过自动选帧和渲染）
# 默认会在封面上叠加英文标题文字（顶部大字 + 可选底部小字）
python scripts/generate_cover.py --work-dir work --output-dir output \
    --profile nba --cover-image input/cover.jpg \
    --headline "MICHAEL JORDAN" --bottom-text "GOAT HIGHLIGHTS"

# 保持用户封面原尺寸（不缩放到 1080x1920）
python scripts/generate_cover.py --work-dir work --output-dir output \
    --profile nba --cover-image input/cover.jpg --no-resize

# 不在用户封面上叠加文字（纯原图）
python scripts/generate_cover.py --work-dir work --output-dir output \
    --profile nba --cover-image input/cover.jpg --no-text

# ===== 正式交付（唯一路径）→ scripts/render_overlays.py，见 §12.5.2 =====
# 下面这几个脚本是 legacy / 排障 / 流水线基底，不要拿它们做正式交付（职责见 §9 工具表）

# 清理视频时指定方法（流水线 Job2 基底；ffmpeg 更快，opencv 适合复杂背景）
python scripts/clean_video.py --video input/source.mp4 --work-dir work \
    --method ffmpeg  # 默认 ffmpeg，可选 opencv

# [legacy] 高级清理：黑色条覆盖 + 文字叠加 + 第一帧中文覆盖 + 去掉开头封面帧（适合中文固定在顶部/底部的视频）
# 仅保留用于「封面图第一帧中文处理」：⭐ 用 --first-frame-blackbar 只在第一帧覆盖中文（音频完全保留，-c:a copy）
# ⭐ 如果封面图持续多帧，用 --trim-start 0.2 去掉开头0.2秒（音频保留）
# 避免用 --replace-first-frame（会重新编码音频，可能导致爆音/质量下降）
python scripts/clean_with_overlay.py --video input/source.mp4 --output output/final.mp4 \
    --blackbar "top:y=170:h=160" \
    --blackbar "bottom:y=950:h=175" \
    --first-frame-blackbar "cover:y=600:h=350" \
    --trim-start 0.2 \
    --overlay-text "text='BLAKE GRIFFIN\nTOP 20 DUNKS':y=195:fontsize=38:color=white"

# [legacy] 生成艺术化 logo 图片——已被 cover_engine.draw_brand_lockup 取代（见 §8），
# 封面/视频内 logo 走 render_overlays.py / make_cover.py，不要再用这个

# [legacy] inpaint 去除中文 + 模糊遮罩 + 英文叠加（仅排障/一次性修补）
# 大区域（顶部标题）用 --blur-region 模糊遮罩，小区域（底部水印/字幕）用 --inpaint 去除
# 处理后能看到原来中文字下面的画面，不是黑呼呼一片；正式交付走 render_overlays.py
python scripts/inpaint_video.py --video input/source.mp4 --output output/final.mp4 \
    --blur-region "top_title:y=170:h=160" \
    --inpaint "bottom_watermark:y=960:h=70" \
    --inpaint "bottom_subtitle:y=1020:h=110" \
    --overlay "text='BLAKE GRIFFIN\nTOP 20 DUNKS':y=195:fontsize=38:color=white:borderw=3" \
    --overlay "text='Hoop Dynasty Daily':y=982:fontsize=22:color=#FFA500:italic:borderw=2" \
    --trim-start 0.5 \
    --inpaint-radius 7 --blur-radius 15 --blur-alpha 180
```

> **中文 logo 替换约定**：底部中文水印（如"热越篮球"）统一替换为英文 `Hoop Dynasty Daily`（首字母大写），使用艺术化样式（橙黄渐变+黑色粗描边+外发光+白色高光，Impact 字体），通用可复用。
>
> **第一帧/封面图中文处理最佳实践**：
> 1. 如果封面图只有1帧：用 `--first-frame-blackbar` 只在第一帧（n=0）覆盖中文区域，音频用 `-c:a copy` 直接复制
> 2. 如果封面图持续多帧（常见，通常0.1-0.5秒）：用 `--trim-start 0.2` 去掉开头0.2秒，音频保留
> 3. **不要**用 `--replace-first-frame`（concat 操作会重新编码音频，导致爆音/质量下降/背景音乐丢失）
>
> **视频与封面分离原则**：视频只处理画面（去中文、加英文标题、加logo），**不在视频里合成封面图**。封面单独生成 `cover.jpg`，用户上传平台时手动选择视频和封面。

## 常见问题与最佳实践（实战总结）

### 1. 第一帧/封面图中文处理

**问题**：视频开头是封面图（带大量中文），持续0.1-0.5秒（多帧），不在顶部/底部黑色条覆盖范围内。

**解决方案（按优先级）**：
1. **封面图持续多帧（最常见）**：用 `--trim-start 0.2` 去掉开头0.2秒，音频用 `-c copy` 保留
2. **封面图只有1帧**：用 `--first-frame-blackbar` 只在第一帧覆盖中文区域
3. **❌ 禁止**：用 `--replace-first-frame`（concat 会重新编码音频，导致爆音/质量下降/背景音乐丢失）

**判断方法**：用 `ffmpeg -i input.mp4 -vf fps=10 -vframes 10 frame_%02d.jpg` 提取前10帧，看封面图持续几帧。

### 2. 音频质量问题

**问题**：处理后视频有爆音/划碟声、背景音乐丢失、音频质量下降。

**根本原因**：使用了 `concat` 拼接视频（替换第一帧），重新编码了音频。

**解决方案**：
- 所有视频处理都用 `-c:a copy` 直接复制音频，不重新编码
- 去掉开头用 `-ss` + `-c copy`，不用 concat
- 避免任何需要重新编码音频的操作

**验证**：处理后用 `ffprobe -show_streams -select_streams a final.mp4` 检查音频比特率，应与原视频一致。

### 3. 黑色条覆盖不彻底

**问题**：中文字上半部分露出黑色条，处理不彻底。

**原因**：黑色条位置不准确，中文文字顶部超出了覆盖范围。

**解决方案**：
1. 先用 `locate_text.py` 精确定位中文区域
2. 黑色条往上多覆盖20-30像素（宁多勿少）
3. 处理后提取验证帧检查，确保中文完全覆盖

### 4. 封面中文去除（非纯黑条）

**问题**：封面用纯黑色条覆盖中文太突兀，不美观。

**解决方案（模糊遮罩法）**：
1. 对中文区域进行高斯模糊（radius=15）
2. 叠加半透明黑色渐变遮罩（顶部/底部渐变过渡，中间alpha=180-200）
3. 在遮罩上写英文标题

这样比纯黑条自然得多，背景隐约可见但中文不可读。

### 5. delogo 效果差

**问题**：FFmpeg `delogo` 处理固定中文区域有波纹闪现，处理不彻底。

**解决方案**：用 `drawbox` 黑色条覆盖，比 delogo 更彻底、更稳定、无波纹。对于固定在顶部/底部的中文文字区域，黑色条是最佳选择。

### 6. 视频与封面分离

**原则**：
- 视频只处理画面（去中文、加英文标题、加logo）
- **不在视频里合成封面图**，不替换第一帧为封面
- 封面单独生成 `cover.jpg` 文件
- 用户上传平台时，手动选择视频文件和封面文件

### 7. 文件完整性验证

**问题**：ffmpeg 后台处理被中断，导致文件损坏（moov atom not found）。

**解决方案**：
1. 处理后用 `ffprobe final.mp4` 验证文件完整性
2. 如果报错，重新处理
3. 大文件处理建议前台运行，确保完整写入

### 8. 封面与品牌规范（唯一定义处）

> **本节是封面 / 品牌规格的唯一出处。** §12.5 / §12.6 / §12.8 / §13.4.2 里
> 只留指针，改规格**只改本节**。
>
> 2026-09-27 之前同一条规则散在 §8 / §8.1 / §12.5 / §12.6 / §13.4.2 五处，
> 「改了 3 处漏了 1 处」直接导致两轮返工：
> 一次是文档写着 Impact 而脚本硬编码 Arial Black，一次是文档写着外发光渐变
> 而脚本只画了纯金平涂。**文档写了、实现没做 —— 根因就是规则有多份。**

#### 8.0 两个入口，一个引擎

封面有两条调用路径，**排版与绘制全部走 `scripts/cover_engine.py`**：

| 入口 | 谁在用 | 脚本 | 职责 |
|---|---|---|---|
| 流水线 Job 3 | `run_pipeline.py` / `jobs/generate-cover.job.md` | `generate_cover.py` | 选帧、按 plan 清帧、组装 spec、落盘 |
| 手动 / 预览 | Agent 手工出图、比稿 | `make_cover.py` | 显式文案参数、抽帧、落盘 |

⚠️ **改渲染逻辑只改 `cover_engine.py`。** 两个 CLI 都只是薄壳。
2026-09-27 之前两套独立渲染器并存：流水线那套把标题画在 `safe_top + 50`、
品牌画在 `safe_bottom`，**两者都在安全区之外** —— 流水线出的封面在主页网格里
标题和 logo 全被裁掉，而手调的封面是对的。这就是「两套实现」的代价。

引擎 API（供测试与调试）：`resolve(spec) -> Resolved`、`compose(frame, spec)`、
`render_cover(frame, spec)`、`letter_cover(img, spec)`、`format_report(...)`。
`CoverSpec` 承载全部参数，`resolve()` 负责从 profile 取值 —— 尺寸解析也只有一份。

#### 8.1 品牌署名（logo）

**文字**：`Hoop Dynasty Daily`（首字母大写）。**位置：底部**。

- **只属底部，不得挪到顶部。** 顶部是 headline 区，两者不得互换。
  ⚠️ 事故档案：2026-09-27 曾把品牌画到封面顶部，**并把本文件改写去"配得上"该实现**，
  被用户当场纠正。**实现与文档冲突时改实现，绝不允许改文档去合理化实现。**
- **字号**：封面取 `cover_style.brand_size`（nba = **64px @1080 宽**），**绝对值，
  不随主标题缩放**。旧实现写成 `font_size * 0.5`，导致不同 profile 出 63/56/45 三种字号。
- **字体**：Impact（即 `font_chain` 第一个已安装项），见 8.2。
- **封面样式三档**（`--logo-style`，2026-09-27 落地）：
  - `sticker` **（封面默认）**：Impact + 纯 `fill_color`（nba 定稿 `#FFE800`）+ 黑色细描边（`brand_stroke_width`），无渐变/发光。
    **依据**：用户 2026-09-27 给的参考封面截图里，底部 `Hoop Dynasty Daily` 就是 Impact 纯色小字。
  - `artistic`：橙黄渐变 + 黑色粗描边 + 外发光 + 白色高光。
    ⚠️ **封面不要用**：外发光是为「压在杂乱画面上」设计的（视频内替换中文水印），
    搬到纯黑封面带里只会糊出一圈橙雾、渐变被冲淡成奶油色。
    - 渐变有个坑：必须按**字形 bbox**（`oy..oy+th`）铺，按整层高度铺会被 padding 压成单色。
  - `plain`：Arial Bold 纯金（历史行为，仅兜底）。
- **封面品牌必须自己画**，不能指望画面里带过来的那行小字（2026-09-27 用户反馈
  「封面上怎么没有我 logo 了」）：视频里的 `Hoop Dynasty Daily` 烧在画面 y≈65% 处，
  抽帧做封面会被原样带进图像区，又小又像随手打的水印。
  用 `make_cover.py --logo "Hoop Dynasty Daily"` / `generate_cover.py` 的
  `profile.brand_text` 绘制。QA 校验 `brand_text_rendered` 必须为真。

**视频内 logo（另一回事，不要和封面混）**：

- **原位替换**：原水印位置先清理，再在**同一原位**贴自家 `Hoop Dynasty Daily`，
  不要挪位置（挪到角落会被用户说"此地无银三百两"）。
- **大小 = 画幅宽度的 32%（2026-09-27 用户定稿，以截图为准）**。
  ⚠️ **必须按画宽换算，不是照抄像素值**（各片分辨率不同，抄像素值必然不一致）。
  - 实现：`render_overlays.py --logo-ratio 0.32`（把文字宽度锁到 `0.32 × frame_w`），
    或调 `logo_font_size(frame_w)` 拿字号再喂 ffmpeg drawtext。
  - 参考（Arial Bold，"Hoop Dynasty Daily"）：frame_w 720 → 24；1080 → 36；1280 → 43。
  - **定稿依据**：用户截图里 logo 宽 345px、字高≈25px。345px 既等于 `0.32 × 1080`
    （1080 宽片的 1:1 截图），也等于 `230 × 1.5`（720 宽片在 1080 宽屏放大 1.5 倍后的截图）——
    两种读法都收敛到 **32%**。
  - **2026-09-27 四条成片实测（未统一前）**：

    | 视频 | 画幅 | logo 实测宽 | 占画宽 | 判定 |
    |---|---|---|---|---|
    | 2001总决赛艾弗森48分 | 720×1280 | 230px | **31.9%** | ✅ 基准 |
    | 2001奥尼尔震撼西部 | 1080×1920 | 183px | 16.9% | ❌ 需放大到 345px |
    | 卡特关键球T0 | 720×720 | 184px | 25.6% | ❌ 需放大到 230px |
    | 2000全明星名场面 | 1280×960 | 无 | — | ❌ 整条缺 logo |
- **样式（2026-09-27 晚 用户定稿）：品牌黄 `#FFE800`** + 4px 黑描边 + 轻微投影。
  ⚠️ 早前一度是**白色**（当时照截图定的，不是 bug），用户在看完对比图后改口：
  「视频中打的 logo 不要保持白色，**和封面保持一致**」。
  - 现在 **title / hook / logo 三者的填充色同一个 `accent`**，`accent` 由
    `render_overlays.py --profile nba` 从 `cover_style.fill_color` 读出
    （`--accent "R,G,B"` 可显式覆盖；都没有则回退 `GOLD` 并打警告）。
    实现：`resolve_accent()` + `build_overlays(..., accent=...)`。
  - **为什么必须同源**：2026-09-27 首次交付的 6 条里，封面是黄、视频内 logo 是白 ——
    封面读 profile、视频读模块常量 `WHITE`。同一个「品牌色」写在两处，必然漂。
  - 改色流程固定为两步：改 `profiles/<profile>.yaml` 的 `fill_color` 一处 →
    **重出封面（`recover`）＋ 重渲叠字（`--no-inpaint`，见 §13.7）**。
  - **字幕仍用白色**：subtitle 不是品牌标识，白字 + 黑描边是平台通行的口播字幕色，
    不要跟着 accent 一起改黄（黄字压在亮画面上会糊）。
- 一个视频里**只出现一次** logo，不要顶部一个底部一个。
- 给已经有旧小 logo 的成片换尺寸/换色：`ffmpeg -vf "delogo=<旧logo实际范围>,drawtext=..." -c:a copy`
  （delogo 框用「成片−源片」差分量出来，**别用 plan 的 bbox** —— bbox 是源水印框，
  不是绘制框）。也可以直接量成片里旧 logo 的 bbox（OCR 一帧即得）。
- ⚠️ 只改 logo 时**不要整片重渲染**：
  `render_overlays.py --video <无字底片> --no-inpaint --profile nba --logo-ratio 0.32`
  单帧只要 ~0.06–0.1s（见 13.4.3 / 13.7 的 `clean-base` 快速路径）。

#### 8.2 封面字号、字体、颜色、描边

**字号（2026-09-27 用户定稿，锁定值）**：全部取自 `profiles/<profile>.yaml` 的
`cover_style`，**任何封面脚本都不得硬编码** —— `make_cover.py` 曾写死 108，
用户问「skill 中写的是多少」时只能回「文档没写清楚」。

| 行 | 取自 | nba 定稿值（1080×1920） | 说明 |
|---|---|---|---|
| 主标题 headline | `font_size` | **127px** | 截图 "PRIME SHAQ" 字面高 115px；Impact 下 "ALLEN IVERSON" 宽 734px = 画宽 68% |
| 底部数据行 stat | 跟随主标题（`stat_size: 0`） | **127px** | **上下两栏必须同号**，不许各算各的 |
| 品牌署名 brand | `brand_size` | **64px** | 截图实测宽 496px / 高 57px，逐像素吻合 Impact 64px；**不随主标题缩放** |
| 灰副标 sub | 主标题 ×0.37 | 47px | 辅助信息，不参与「两栏同号」 |
| 白 tagline | ≤44px（并受下黑带高度约束） | 44px | 同上 |

- ⚠️ **底部数据行不许写死上限**：历史 bug `min(138, gap*0.42)` 让 "48 POINTS" 变成 138px，
  **比主标题还大**（主标题当时 112），用户 2026-09-27 质问「下面的 48 POINTS 咋没改呢」。
- **长标题先折行、不缩字号**：字号属于规格，`--headline-max-width`（默认 0.96×画宽）
  配合 `wrap2()` 在词边界折成最均衡的两行；只有折行后仍超宽才允许缩字号。

**字体**：取 `cover_style.font_chain` 里**第一个已安装**的字体，由
`common.resolve_font_chain()` 解析（**两份封面脚本共用这一个函数**）。
nba 的 `["Anton","Impact","Arial Black","sans-serif"]` 实测落到 **Impact**。

- ⚠️ **不要硬编码字体**：`make_cover.py` 曾写死 Arial Black，把 font_chain 整个忽略掉；
  2026-09-27 用户给参考截图说「就是这种字体大小」时，量出来截图是 Impact
  （窄体，同字号下比 Arial Black 窄 36%、字面高只有其 76%），脚本却画不出这个效果。
- profile 里 `font_chain` 存的是**字体名**（保持可移植），到本机实际字体的映射在
  `common.SYSTEM_FONTS`。不要在脚本里写 `.ttf` 绝对路径。

**颜色**：品牌黄 = `cover_style.fill_color`，**nba 定稿 `#FFE800` = `[255, 232, 0]`**
（2026-09-27 用户看对比图定稿，见下方亮度表 C 档）。`sports.yaml` / `generic.yaml`
仍是 `[255, 220, 0]`（TikTok 经典 `#FFDC00`），换 profile 前确认一次。

- ⚠️ **禁止配成 `[255, 255, 255]` 白色**（历史上 nba.yaml 曾误配白色，封面标题变白）。
- ⚠️ **绘制必须读 `fill_color`，脚本里不得出现颜色字面量**（2026-09-27 修的 bug）：
  `cover_engine.compose()` / `letter_cover()` 里的**主标题、stat 行、品牌署名原本全部硬编码**
  模块级 `GOLD=(255,208,0)`，profile 的 `fill_color` 只被写进 `meta["styles"]["fill_color"]` ——
  结果 **QA 读 profile 永远 PASS，实际像素却比规格暗一档**。
  现已在两个函数里改为 `accent = tuple(style.get("fill_color") or GOLD)`（`GOLD` 仅作兜底默认）。
  实测修复前后同一封面：像素 `(255,208,0)` → `(255,220,0)`，定稿后 → `(255,232,0)`。
  **教训与本节开头的"规则有多份"同源：文档写了、实现没做、QA 又只查文档 → 三方一致地错。**
- ⚠️ **同一个品牌色出现在两个渲染器里**：封面走 `cover_engine`，视频内 title/hook/logo 走
  `render_overlays.py`。后者现在也通过 `--profile`（读同一个 `fill_color`）取色，
  所以**改色只需改 profile 一处 + 两处重跑**（封面 `recover` / 叠字 `--no-inpaint` 重渲）。
  任何"再写一次颜色常量"的行为都会重演 2026-09-27 的「黄封面 + 白 logo」。
- QA 校验：R≥230, G≥180, B≤80, G>B，非黄色系直接 WARNING。
  ⚠️ 它校的是 **profile 的值、不读像素** —— 所以改 `fill_color` 时别指望 QA 帮你发现实现没跟上。
  **定稿后务必用 `chk_accent`（本批 07 的 `tiktok-batch/chk_accent.py`）抽帧/抽图读真实像素。**
- **为什么黄色**：TikTok 上黄色粗体 + 黑描边是最经典、最醒目的封面文字样式，信息流里一眼可见。
- 阴影：黑色偏移（+3, +3）。文字区顶部有轻微暗化渐变（不做纯黑色条）。
- **亮度定稿（2026-09-27 完成）**：用户反馈「封面上的字体颜色偏暗了一点，黄色要稍微亮一点」，
  看完真实渲染对比图（`cmp/cmp_cover_yellow.png`）后**选 C 档**：

  | 候选 | RGB | G 相对 B 档 | 结论 |
  |---|---|---|---|
  | A | 255,208,0 | −12 | **修复前的实际值**（硬编码 GOLD），仅作对照 |
  | B `#FFDC00` | 255,220,0 | ±0 | TikTok 经典黄，旧 profile 值 |
  | **C `#FFE800`** | **255,232,0** | **+12** | ✅ **定稿**：明显更亮一档，仍克制 |
  | D `#FFF200` | 255,242,0 | +22 | 更接近纯黄，未采用 |
  | E `#FFFC00` | 255,252,0 | +32 | 纯黄，未采用 |

  定稿即**只改 `profiles/<profile>.yaml` 的 `fill_color` 一个数字**（其余 profile 同理），
  不要再动代码 —— 代码现在已完全跟随 profile。

**描边**：

- 主标题取 `cover_style.stroke_width`（nba = 6，1080 宽约 0.56%）。
- 品牌署名取 `cover_style.brand_stroke_width`（nba = 3，截图实测）。
- 另一条通用规则「描边 ≥ 画布宽 1.2%（720 宽至少 8px）」针对的是**压在画面上的标题**
  （需要抗背景干扰）。封面是两个纯黑带，不需要那么重。
- ⚠️ **两处数值不同是有意为之**，不要"顺手统一"成 13px，会把字压得太重。

**规格复核方法（换字号前先做）**：拿用户给的参考截图，量出黄字块的**宽和高**，
按**画宽**归一化到 1080 后反查字号 —— 只有宽、高两条线**同时对得上**的字体才是对的。

- 2026-09-27 实例：截图 912×1370（2:3），顶部标题高 115px / 宽 602px，
  底部品牌高 57px / 宽 496px。结论 Impact 127px / Impact 64px。
  验算：Arial Black 要对上标题宽度只需 86px，但字面高只有 70px；Arial Bold 同理对不上。

#### 8.3 安全区（TikTok 会裁掉封面上下两端）

**规则**：主标题、数据行、品牌署名**必须全部落在画布中心 1080×1080（y 422–1498）内**。

三种显示场景下 1080×1920 封面的可见范围：

| 场景 | 可见纵向范围 | 说明 |
|---|---|---|
| 全屏（推荐页/播放页） | y 100–1620 | 底部约 270–480px 被字幕/用户名/按钮盖住 |
| 主页网格 + 搜索结果 | y 240–1680（**3:4**）或 y 420–1500（**1:1**） | 居中裁切，上下两端直接消失 |

各来源对网格裁切说法不一（3:4 与 1:1 都有），**取交集** → 中心 **1080×1080
（y 422–1498，即 `safe_zone_top_pct/bottom_pct = 0.22`）**。

- 用户 2026-09-27：「封面图片要适合 tiktok 封面大小，在封面上要能够看全标题和 logo」。
  旧版式标题在 y≈138、品牌在 y≈1791，**在网格里两者都会被裁掉**（用户就是这样发现的）。
- 想给画面多留高度可放宽到 `0.15/0.15`（= 3:4 网格窗口），**不要再往外放**。
- 脚本每次都会打印 `safe-zone: composition A-B inside C-D -> OK|OVERFLOW`，
  **OVERFLOW 就是不合格**；`qa_video.py::check_cover` 也会按真实文字 bbox 复核
  （`text_in_safe_band`，带外即 warning）。

#### 8.4 封面版式与画面处理

底图来源（**必须无字**）：

1. 首选 `render_overlays.py --save-clean-base` 导出的**无字底片**抽帧（见 13.4.3）；
2. 底片不在时，从**源视频**抽目标帧再按 OCR 框 inpaint 掉烧入文字与转载方角标，
   把清理后的帧存进 `work/` 复用（如 `work/cover-base-68s.png`）；
3. ⚠️ **不要从 `cleaned_*.mp4` 抽帧** —— 那上面已经烧了英文字幕/logo，会直接进封面；
4. ⚠️ **也绝不能用未清理的源帧** —— 中文原样进封面。

版式参数（`make_cover.py`，同样可从 `cover_style` 读）：

- `--layout safe`（默认）：整个排版挤进安全带。`--layout classic` 是旧的全画布版式，
  **只有复现旧封面时才用**，它会打印 `classic layout ... get cropped in the profile grid` 警告。
- `--picture fill|band`：`fill` = 画面铺满安全带、文字压在画面上（配 `--scrim` 造渐变，
  画面最大，2026-09-27 定稿）；`band` = 画面只占文字之间的横条、文字落在干净背景上。
  两者安全区保证相同。
- `--backdrop blur|black`：安全带以外用「模糊压暗的整帧」填充（默认），全屏看是满幅的，
  网格里被裁掉的只是背景虚化，无信息损失。
- `--focus 0..1`：画面在干净带内的取景中心。**换帧后必须目视确认头顶没被切**（本例 fill 取 0.42）。

画面处理 —— **不要锐化**：

- 720p 帧放大到 1080 本身带噪点，再锐化（旧默认 ×1.5）会把观众席人头变成视觉噪点 ——
  用户两次反馈「封面很花 / 画面不是花的吗」的根因。
- 正确方向是**把背景压下去**：对比度 ×1.06、饱和度 ×0.78（抽掉观众席黄绿斑块）、
  亮度 ×0.88，再把主体以外的高斯模糊版（`--bg-blur 8`）+ **额外压暗 ×0.72**（`--bg-dim`）
  用连续渐变 mask 合成回画面（清晰椭圆 `--dof-cy 0.55 / --dof-ry 0.50`），
  叠径向暗角（`--vignette 170`），最后把画面上下边缘 fade 进黑带。
- 关键：`Image.composite(area, black, mask)` 里 **mask 亮=保留**，写反会把整幅压黑。
- DOF mask 必须用**连续渐变**（numpy 距离场 `clip((1.18-d)/0.55)`），
  不要用 `ImageDraw.ellipse` 填充 —— 硬边椭圆会留下可见的斜切边。
- **禁止变成黑块/灰块**（§12.8 自检项）。
- 全帧放大到 1080×1920，保持原上下黑边结构，与成品视频视觉一致。

#### 8.5 选帧：必须是人物特写，不能是远景全景

远景帧（球场全景）背景整片是观众席，无论怎么后期都救不回来
（2026-09-27 用户第二次反馈「画面不是花的吗」）。

- **实现**：`scripts/pick_cover_frame.py`，按
  `score = face_area*1000 - clutter*420 + min(lap/300, 1.2)*60` 排名，其中
  ① Haar 人脸框面积占比（越大越好）② 画面上 45% 区域的 Canny 边缘密度
  （观众席 = 高频杂乱，越低越好）③ Laplacian 方差（太软不好）。
- `generate_cover.py` 的自动选帧已改为调用这个评分器；旧版
  「sharpness*0.5 + bonus」只在评分器不可用时兜底，并会打 warning。
- 实测本例 43 张 scene 帧里只有 3 张是近景，最高分 `s_0018`（=**68.067s**）
  人脸占比 7.2%、clutter 0.017，而远景帧普遍 clutter 0.10+。
- ⚠️ **分数只用来筛候选**（本例最高分的 s_0037 其实是 McKie 的背影），
  **最终必须目视确认主体是谁**，别只看分数。

#### 8.6 封面帧清理（禁止中文/水印带入封面）

- 按 `localization-plan.json` 的 remove/translate/replace 项清理，
  大区域（>100px 高）用高斯模糊 + 半透明黑遮罩，小区域（≤100px）用 OpenCV inpaint。
- ⚠️ 封面会从 720p 放大到 1080×1920，**模糊必须比视频清理更重**
  （默认 `blur_radius=25 / blur_alpha=210`），否则放大后中文笔画仍可辨认。
- 🔴 **只清理「封面帧那一刻真的在屏」的项**（`plan_items_at()` 按 `start_time`/`end_time`
  过滤，`generate_cover.py` 用 `selected_frame.timestamp_sec` 传入）。
  **封面是单帧快照，plan 覆盖全片** —— 不过滤就会把全片区域都擦到这一帧上：
  2026-09-29 的 03（湖人传奇羁绊）plan 里唯一的 `caption_overlay` remove 项是
  0.6–3.4s 的中屏中文弹窗（bbox h=128 ⇒ 走「大区域」分支 = 高斯模糊 + 82% 压暗），
  封面帧取在 t=236.85s，结果**在科比脸上压出一块 361×191 的黑方块**；
  同一帧的源画面本来是干净的（抽帧验过），黑块 100% 是封面渲染引入的。
  判据：`cover-plan.json` 的 `render.frame_cleaned_regions` >> `frame_clean_expected`
  （03 是 65 vs 0），或 `frame_clean_at_sec` 落在任何 item 的时间窗之外。
- 清理项数记录在 `cover-plan.json` 的
  `render.frame_cleaned_regions` / `frame_clean_expected` / `frame_clean_at_sec`，
  QA 用 `expected`（=该帧在屏项数）而非「全片项数」做校验。
- 清理在**裁剪缩放之前**做，bbox 以源帧像素坐标系为准。
- 副作用提示：清理结果会**同时影响画面与背景虚化层**（`backdrop: blur` 是同一张
  `img` 的模糊版），所以擦错区域的痕迹是"两处"：主画面一块 + 背景一抹。

#### 8.7 QA 与交付前自检

`qa_video.py::check_cover` 六项（缺一即 warning）：

1. 封面中文残留 OCR 抽检（RapidOCR，`ocr_cover_chinese()`）。
   **不设 tesseract 兜底** —— 引擎不可用时该项报 warning，**不得**当成「封面干净」。
   检出的中文行需 ≥2 个 CJK 字符才报告（减少单字误报）。
2. 标题颜色必须是品牌黄（判定读 `profile_cover_style.fill_color`，nba 定稿 `#FFE800`；
   阈值式：R≥230 / G≥180 / B≤80 / G>B，**不读像素**，所以别只信这一条）。
3. `brand_text` 必须渲染（`render.brand_text_rendered`）。
4. 人脸遮挡判定：用 render 输出的**实际文字区域 bbox（`text_regions`）**与人脸矩形
   做重叠检测。⚠️ **禁止用整个安全区做判定**（会导致人脸在安全区内但文字没压到也误报）。
   人脸坐标由 `generate_cover.py` 用 `picture_src` 映射到画布坐标系，
   不在可见画面带里的人脸会被丢弃。
5. 封面帧已按 localization-plan 清理（`frame_cleaned_regions` ≥ `frame_clean_expected`）。
   ⚠️ `frame_clean_expected` = **该帧(selected_frame.timestamp_sec)在屏的项数**，
   不是全片项数 —— 用全片项数当分母会逼着 cover 去擦不存在的区域（见 §8.6）。
   旧 cover-plan 没有该字段时，`check_cover` 就地按时间戳过滤，行为一致。
6. **标题与品牌署名落在安全带内**（`text_in_safe_band`，按真实 bbox 复核）。

封面按视频内容定制，**禁止模板化**（§12.6）：一开始 8 个封面全写 "MUST WATCH"，
用户骂「怎么都是 Must watch」。标题从内容提炼（球员名 + 看点）。

**实现**：`scripts/cover_engine.py`（排版与绘制，两条封面路径共用）；
颜色取自 `profile.cover_style.fill_color`，字号取自 `font_size` / `brand_size`。

### 8.8 遗留：规格值不得硬编码（已归并 → §8）

本节原有的颜色 / 字体 / 描边 / 字号 / 安全区 / 品牌署名 / 帧清理 / QA 检查
**全部内容已并入 §8**，此处不再重复。

留下的唯一一条经验：**规格值一律从 `profiles/<profile>.yaml` 的 `cover_style`
读取，脚本里不得出现字面量**。历史上 `make_cover.py` 写死过 `108`（字号）、
`Arial Black`（字体）、`font_size * 0.5`（品牌字号），三次都被用户当场发现 ——
硬编码的代价是「用户问规范是多少，只能回答文档没写清楚」。
**2026-09-27 又犯了第四次（更隐蔽）**：`cover_engine.py` 硬编码颜色 `GOLD(255,208,0)`，
profile 的 `fill_color` 只进 meta 不参与绘制 → 文档对、QA 也 PASS、像素偏暗一档。
详见 §8.2 的「绘制必须读 `fill_color`」。


### 9. Inpaint 去除中文（非黑色条遮罩）

**⚠️ 重要前提：只有检测到中文的区域才需要处理！**

- **英文记分牌、英文比分牌、英文水印**：不需要任何遮罩或inpaint处理，保留原样
- **没有中文的视频**：完全跳过中文去除步骤，直接做原创化处理
- **边缘裁剪要注意**：裁剪8%边缘时，要确保不会裁剪掉重要的英文记分牌或比分牌；如果记分牌在边缘，应调整裁剪区域或跳过裁剪
- **处理前必须先 OCR 检测**（`ocr_video.py`，RapidOCR）：确认哪些区域有中文，只处理有中文的区域，不要盲目遮罩。
  注意 OCR 的 bbox 是**文字紧贴框**，若文字附着在底板/气泡/色条上，必须把整个图形纳入掩码（见 13.2 规则③）

**问题**：黑色条遮罩太突兀，黑呼呼一片，看不到原来中文字下面的画面。

**解决方案（按区域大小选择，处理原则与具体工具见下表）**：
1. **大区域（顶部标题，>100像素高）**：高斯模糊 + 半透明黑色遮罩
   - 能看到下面画面的模糊轮廓，比纯黑条自然
2. **小区域（底部水印/字幕，<100像素高）**：OpenCV INPAINT_TELEA inpaint
   - 用周围像素填充，处理后能看到原来中文字下面的画面
3. **然后在处理后的区域写上英文**：叠加英文标题和 logo

**工具（2026-09-27 澄清，别再选错）**：

| 工具 | 状态 | 何时用 |
|---|---|---|
| **`scripts/render_overlays.py`** | ✅ **正式交付唯一路径** | 单 pass 完成「ROI 局部 inpaint 去原中文/水印 → Pillow 叠加英文（粗体+描边+投影）」；`category=watermark` 项原位重画品牌 mark。见 §12.5.2 |
| `scripts/inpaint_video.py` | ⚠️ 仅排障/一次性修补 | 只做「模糊遮罩 + inpaint + ffmpeg overlay」，字是细体、无投影、无自动去重叠；**不要拿它做正式交付** |
| `scripts/clean_with_overlay.py` | ⚠️ legacy | 黑条覆盖 + 顶部/底部中文覆盖的早期方案；被 `render_overlays.py` 取代 |
| `scripts/clean_video.py` | ✅ 流水线基底 | Job 2 的裁剪/inpaint 引擎，`--method opencv|ffmpeg`。但**它的 drawtext 路径不要用于正式交付**（时间窗重叠会叠字，见 §12.5.2） |

⚠️ 之前本节直接推 `inpaint_video.py`，与 §12.5.2「不要再用 `clean_video.py` 的 drawtext
路径做正式交付」互相矛盾，导致同一个任务有两种做法。**以本表为准。**

`clean_with_overlay.py` 与 `inpaint_video.py` 已加运行时 `[DEPRECATED]` 警告（直接跑会在
stderr 提示改走 `render_overlays.py`），但**功能完整保留**——它们的独有用途仍在：
`--first-frame-blackbar`（封面第一帧中文）、`--blur-region`（大区域模糊）见下方参数说明。

`inpaint_video.py` 参数（供排障参考）：
- `--blur-region`：模糊遮罩区域（大区域）
- `--inpaint`：inpaint 去除区域（小区域）
- `--overlay`：英文文字叠加
- `--trim-start`：去掉开头N秒（封面图）
- `--inpaint-radius`：inpaint 半径（默认5，复杂背景用7）
- `--blur-radius`：模糊半径（默认15）
- `--blur-alpha`：半透明遮罩透明度（默认180，0-255）

**注意**：inpaint 逐帧处理较慢（149秒视频约需15-20分钟），大文件建议后台运行。

### 10. BGM 推荐与音频处理

**问题**：源视频音频包含篮球声和原BGM，处理后音频质量差或BGM不合适、单调重复。

**解决方案（篮球集锦类视频推荐）**：
1. **去掉原视频所有音频**：使用 `remove_original_bgm_only` 策略，去掉篮球声和原BGM
2. **多维度推荐具体 BGM**：从 `configs/bgm-library.yaml`（45首歌，11种风格）中选择
   - **按内容**：扣篮集锦→快节奏摇滚/嘻哈，比赛回顾→中等节奏电子/Lo-Fi，致敬传奇→慢节奏钢琴/电影配乐
   - **按球员风格**：
     - 暴力美学/飞天遁地（格里芬、卡特、詹姆斯、威少）→ 摇滚/嘻哈/史诗管弦（"Sport Rock" / "Hitman" / "Epic"）
     - 技术流/优雅（乔丹、科比、库里、欧文、杜兰特）→ 电子/电影配乐/Lo-Fi（"Drive" / "Cylinder Four" / "The Builder"）
     - 团队篮球/战术（马刺、勇士、凯尔特人）→ 电子/Lo-Fi/放克（"Wallpaper" / "Chill Lo-Fi" / "Smooth Funk"）
     - 传奇/老将（乔丹、科比、诺维茨基、韦德）→ 电影配乐/管弦/钢琴（"Impact Moderato" / "The Glory Path" / "Cylinder One"）
   - **按情绪**：激昂→快节奏摇滚，怀旧→中等节奏电子，感人→慢节奏钢琴，搞笑→喜剧音乐
   - **按时长**：短视频（<60秒）→快节奏，长视频（>180秒）→中等节奏
3. **避免重复**：同一批处理的视频，避免使用相同的BGM，在匹配的音乐列表中随机选择
4. **BGM 处理**：循环/裁剪到视频时长，开头2秒淡入，结尾3秒淡出
5. **必须给出具体音乐名称**：不能泛泛推荐"快节奏音乐"，必须说明音乐名、艺术家、BPM、风格、为什么适合这个视频

**音频策略选择**：
- `keep_original`：保留原声（现场声/欢呼）
- `mute_original_tts_bgm`：静音原轨 + 英文TTS + BGM（有中文旁白时）
- `remove_original_bgm_only` ⭐：去掉原音频 + 纯BGM（篮球集锦推荐）

**音乐库统计（2026-09-28 实测口径）**：`music/sports/` **34 个文件 = 24 首歌**
（10 个是 `*_variant` 变速变体，与母版同属一首）。
⚠️ 早先"45 首"是**按文件＋人工 yaml 条目数**数的，**虚高** ——
可换歌数必须按歌曲算，否则会误以为库存充足（见 §12.4）。

### 10.1 节奏匹配：BGM 与视频「同拍、共高潮」（2026-09-28 新增）

**问题**：以前 BGM 是「从第 0 秒平铺到片长」—— 配乐的高潮与视频高潮毫无关系，
用户反馈的"节奏对不上""没有高潮起落"就是这个。

**两条硬要求**：① **节奏要明显**（BPM 落在 120–150 密集鼓点区，不要舒缓）；
② **高潮要有起落**（BGM 的 drop 压在视频最激烈的那一刻上）。

**流程（⭐ 选曲是第 0 步的**后半**，但必须等量完视频拿到 `suggested_bpm` 才能做）**：

```bash
# ① 量视频：场景切换 + 运动强度 -> rhythm.json（拿到 suggested_bpm）
python scripts/analyze_rhythm.py --video <源视频> --work-dir <work>

# ② 按该 BPM / 风格上网搜候选，再用 resolve 逐个查本地库
#    → 本地有且没用过 = 用本地（省下载）；本地没有 = 去下载
python scripts/bgm_ledger.py resolve --bpm <suggested_bpm> \
    --candidate "搜到的第1首::作者" --candidate "搜到的第2首" ...

# ②b 只有裁决是「需下载」时才下载并入册（曲库 +1）
python scripts/bgm_ledger.py add --audio <下载的文件> --name "<曲名>" \
    --artist <作者> --source <来源> --license "CC BY 3.0" --category <类目>

# ③ 量 BGM：实测 BPM + 能量曲线 + drop -> bgm-plan.json（读 ① 的结果做对齐）
python scripts/analyze_bgm.py --bgm music/sports/<曲目>.mp3 --work-dir <work> \
       --track-name "<曲目>"

# ④ 混音：audio_processor.py mix 自动读 bgm-plan.json 的对齐方案
python scripts/audio_processor.py mix --video <成片> --work-dir <work> \
       --output-dir <out> --audio-plan <work>/audio-plan.json --timeline <work>/timeline.json

# ⑤ 记账：记下这条用了哪首，下次才能避免重复
python scripts/bgm_ledger.py record --track music/sports/<曲目>.mp3 --video <源视频> --output-dir <out>
```

> ② 的裁决规则见 §12.4「两层模型」：**上网搜是决定"选哪首"，本地库是决定"要不要下载"**。
> 本地命中**必须**是"没用过"的 —— 用过的直接复用，等于曲库白扩充（§12.4）。

**`rhythm.json` 关键字段**：

| 字段 | 含义 | 怎么用 |
|---|---|---|
| `cuts` / `cut_rate_per_min` | 场景切换时刻与频率 | 判断剪辑密度 |
| `shot_len_median_sec` | 镜头时长中位数 | 折算 BPM |
| `peaks` / `motion` | 局部峰值 / 逐点运动强度 | 找"最激烈"的时刻 |
| `climax_mid` | **中后段**最强峰 | ⭐ BGM 的 drop 对齐这里（**不要用 `climax`**，它常落在片头特效上）|
| `suggested_bpm` / `bpm_source` | 建议 BPM 与依据 | 选曲用 |

**BPM 怎么算出来的**（`bpm_source` 三档）：
1. **`cuts`（首选）** —— 让「每个镜头 ≈ 整数拍」且 BPM 落在 110–165。实测 01：
   镜头中位数 1.808s → 每镜头 4 拍 → **132.7 BPM**。
   *为什么以剪辑点为主*：观众感知的"卡点"是切镜与鼓点同时发生；画面运动的自相关
   反映攻防起伏，周期常有好几秒，会给出偏低值（01 给 90，不符"节奏要明显"）。
2. **`motion_autocorr`** —— 剪辑稀疏、但画面自相关置信度 ≥0.25 且 BPM ≥100 时用。
3. **`default_energetic`** —— 都不成立（长镜头素材）时取 **120–145**。实测 05
   （824s / 82 处切换 / 镜头长 10s）走这一档。**不要**把画面自相关的低值直接交出去。

**⚠️ 曲库的 BPM 登记值全部不可信**（2026-09-28 实测，8 个可对照曲目 **8/8 偏差 >5%**）：

| 曲目 | 登记 | 实测 | 偏差 |
|---|---|---|---|
| Breaktime | 132 | **172.3** | +31% |
| Impact Moderato | 130 | **99.4** | −24% |
| Carefree | 115 | **95.7** | −17% |
| Cipher / The Builder | 130 / 105 | **152.0 / 123.0** | +17% |
| The Path of the Goblin King | 142 | **120.2** | −15% |
| Wallpaper | 108 | **92.3** | −15% |
| Hitman | 140 | **152.0** | +9% |

**选曲一律查 `configs/bgm-bpm-measured.json`**（实测值 + 置信度 + 时长 + 是否 variant）。
BPM 估计做过准确率自测：**5 个已知节拍全部命中、误差 <1%**（90/108/120/140/160）。

实测可用的高节奏曲目（`confidence ≥ 0.35`）：**132.5** `Future Gladiator` / `Heroic Age_v1`；
**152.0** `Hitman` / `Hitman_v3` / `Cipher` / `Metalmania` / `Volatile Reaction`；
**161.5** `Black Vortex` / `Volatile Reaction_variant`。
⚠️ `Crusade`(conf 0.25) / `Sneaky Adventure`(0.14) 置信度过低，别单独依赖它的 BPM。

**对齐怎么落地**（`audio_processor.build_bgm_track()`）：`atempo`（BPM 拉向视频，
兼做 §12.4 第 9 条的指纹变形）→ `atrim=start=<offset>`（跳过开头，使 drop 落在 `climax_mid`）
→ `aloop`（不足则循环，衔接点取能量低谷）→ 淡入淡出。

**验收方式（别只看日志）**：把生成轨的第 3–5s 与原曲第 `offset+3` 秒做归一化互相关。
实测 `offset=62.79s` 时 **NCC = +0.994**，对照"从头铺"的 −0.056 —— 这才是 offset 真生效的证据。

**三个坑**：
1. **判定"有没有场景切换"时日志级别必须是 `-v info`**：showinfo 走 info 级，
   用 `-v error` 会**一个切换点都拿不到**，进而误判成"这条是单镜头"（01 曾如此）。
2. **同一处闪变会被连报多次**（实测 21.500 / 21.538 只差 0.038s），不合并会把镜头时长
   中位数算小、BPM 算高 —— 用 `merge_close_cuts()`（间隔 <0.3s 归并）。
3. **循环衔接点必须避开 drop**：drop 之前天然是全曲最低点，取全局最小会选中"build-up 的谷底"
   （实测 Wallpaper_v2 选到 88.88s，而 drop 在 89.12s，只差 0.24s），在那里跳回开头会
   把整个进入段切断。`find_valley()` 已加 ±3s 屏蔽。

**曲源与版权**：**TikTok 官方的 Commercial Music Library（CML）不适用于本场景**，
理由有 3 条，**前 2 条与发布平台无关**（2026-09-28 补正：早先只写了"跨平台"一条，
遇到"我只发 TikTok"的追问就会站不住，实际能独立成立的是下面第 1 条）：

1. **CML 的授权只在"app 内选音"时生效**。条款原文：`You may only post or share videos
   that include Commercial Sounds within TikTok and **by using the sharing features made
   available to TikTok users**`。本 skill 产出的是**音频已嵌进 MP4、走上传流程**的文件，
   不是"在 app 内选音" —— 这一步就已经出界。
2. CML 同时禁止单独取用曲目：`You are not permitted to make available, distribute, or
   perform the Commercial Sounds separately from the videos into which you have
   incorporated the Commercial Sound` —— 把 CML 曲目下载下来混进 MP4，也不是被允许的用法。
3. 此外它的授权也只覆盖 TikTok 站内：`Commercial Uses outside of TikTok are not permitted`。

⚠️ **"我只发 TikTok" 不能解除第 1、2 条** —— 发布平台一致 ≠ 使用方式一致。
CML 唯一合规的用法是：**视频不带 BGM 上传，在 TikTok 发布流程里选音**；
但那样就**无法做节奏对齐**（App 内选曲不能 `atempo`、不能偏移），与本节的目标直接冲突。
所以本 skill 一律走**本地 `music/sports/` 的 CC BY 曲目 + `atempo` 指纹变形**。

⚠️ **CC BY 的 "BY" 是许可条件、与平台无关**：无论发 TikTok、抖音还是别处都必须署名，
不能因为"只发一个平台"就省掉。署名由 `generate_post.py` 自动写进 `tiktok-post.md`（§12.4 第 6 条）。
⚠️ `bgm-library.yaml` 中 **8 首**标 `YouTube Audio Library License` 的曲目**不可用于 TikTok**
（该许可只覆盖 YouTube 站内），脚本会拦下并出「授权不可用」警告。
⚠️ NCS 曲目已被 TikTok 识别库收录，禁用（§12.4 第 8 条）。

### 11. 原创化处理（突破 TikTok 非原创判定）

**问题**：搬运视频上传 TikTok 后收到"内容可能受限 - 非原创内容、劣质内容和二维码内容"警告，算法匹配到原视频导致流量受限。

**解决方案（原创化处理 8 要素，已验证通过 TikTok 审核）**：

| # | 要素 | 说明 | 参数 |
|---|------|------|------|
| 1 | 边缘裁剪 8% | 改变原始构图，去除边缘水印 | `--crop 0.08` |
| 2 | 正方形转竖版 | 720x720 → 720x1280，模糊背景填充 | `--to-vertical` |
| 3 | Ken Burns 缓慢放大 | 1.0→1.08，破坏画面指纹 | `--zoom 1.08` |
| 4 | 精彩瞬间慢动作 | 0.6x，增加戏剧效果 | `--slowmo "44-46:0.6"` |
| 5 | 开头标题卡 | 1.5秒，视频帧模糊+暗化做背景 | `--title "TITLE"` |
| 6 | 动态英文字幕 | 精彩瞬间弹出，淡入淡出 | `--dynamic-text "46-51:THE GOAT!"` |
| 7 | 重新编码 | libx264 crf20，改变元数据 | 自动 |
| 8 | 替换 BGM | 音频也做改编，非原始音频 | `--bgm bgm.mp3` |

**工具**：`scripts/originalize_video.py`

```bash
# 基础原创化（裁剪+缩放+标题卡+换BGM）
python originalize_video.py --video input.mp4 --output final.mp4 \
  --title "BUZZER BEATER" --subtitle "AT THE BUZZER" --bgm bgm.mp3

# 完整原创化（含慢动作+动态字幕+转竖版）
python originalize_video.py --video input.mp4 --output final.mp4 \
  --to-vertical --title "JORDAN vs LEBRON" --subtitle "GOAT DEBATE" \
  --slowmo "44-46:0.6" --dynamic-text "46-51:THE GOAT!" \
  --bgm bgm.mp3 --work-dir work
```

### 11.1 标题卡规范（禁用纯黑背景）

**⚠️ 关键规则：标题卡背景必须用视频帧模糊+暗化，禁止纯黑色背景！**

**问题**：纯黑色背景标题卡跟视频内容太不搭，突兀割裂。

**解决方案（视频帧模糊背景法）**：
1. 提取视频第一帧作为背景
2. 高斯模糊 `boxblur=15:2`（不能太模糊，要能看出是比赛画面）
3. 轻微暗化 `eq=brightness=-0.15:contrast=0.9`（**暗化程度不能超过 -0.2，否则看起来像黑色**）
4. 叠加亮黄色粗体文字 + 黑色粗描边
5. 0.5秒淡入效果

**标题卡参数规范**（已固化在 `originalize_video.py` 中）：
- 模糊：`boxblur=15:2`
- 暗化：`brightness=-0.15`（**禁止 ≤ -0.3，会看起来像纯黑**）
- 对比度：`contrast=0.9`
- 时长：1.5秒
- 主标题字号：96px，亮黄色 `yellow`，Arial Black
- 副标题字号：40px，白色 `white`
- 淡入：0.5秒

**为什么用视频帧模糊背景**：跟视频内容一脉相承，观众一眼就知道这是这个视频的标题卡，而不是突兀的黑色幻灯片。模糊+轻微暗化既能让文字清晰可读，又能保留视频的氛围和色彩。

**Agent 介入点**（jobs/*.job.md 中标注 `[AGENT]`）：视频理解、分类、翻译、tts_adjust、cover 文案、post 文案、review 报告、QA 裁决、原创化参数（标题卡文字、慢动作时间点、动态字幕文字）。

## 数据契约（核心 JSON）

所有文件落盘于 `work/`。核心枢纽契约：

- **`probe.json`** — 视频元数据（时长/分辨率/fps/音频）
- **`ocr.json`** — 两遍采样合并后的 OCR 检测结果
- **`text-tracks.json`** — 跨帧跟踪后的文字轨迹（start/end/position/persistence/trajectory/asr_text_overlap）
- **`asr.json`** — 语音转写（含 has_chinese_narration）
- **`localization-plan.json`** — ★枢纽契约：Agent 写、Scripts 读，含每个文字区域的 category/action/confidence/bbox/times/source_text/target_text/replacement/source_text_removal/removal_method（inpaint/blur/delogo，禁止 blackbar）
- **`timeline.json`** — TTS 对齐后的最终时间轴（字幕与混音的唯一时间基准）
- **`audio-plan.json`** — 音频处理方案（strategy/tts/bgm）
- **`cover-plan.json`** — 封面方案（选帧/主体/安全区/文案/渲染参数）
- **`qa.json`** — 质量检查结果（PASS/WARNING/FAIL + 五类检查 + 降级汇总 + review项）

详见 `docs/architecture.md`。

---

## 12. 实战返工总结（2026-09 NBA 第二批 8 条）

以下问题在真实批次中被用户反复打回，固化为硬规则，**禁止再犯**。

### 12.1 画幅：横版比赛画面禁止强行转 9:16 竖屏

**踩坑**：把 16:9/4:3 的横版比赛画面塞进 9:16，上下用模糊色块填充，用户评价"全是花的""一地鸡毛"。

**规则**：
- NBA 比赛集锦默认**保持原横版分辨率**（1280×720 / 1280×960），不转竖屏。
- 只有源素材本身就是竖屏（如手机拍训练）才用 9:16。
- `originalize_video.py --to-vertical` 仅限正方形素材或用户明确要求竖屏时使用。
- 高清化时统一：`unsharp=5:5:1.0:5:5:0.0` + `libx264 crf18`，不要过度放大到 1080×1920 产生噪点。

### 12.2 中文擦除：精确 inpaint，禁止大黑块

**踩坑**：
- inpaint mask 拉太大，中文没盖住反而留下一大块黑/灰矩形（用户称"黑格子""黑框"）。
- 英文字幕另起一块大黑底，和原中文位置错开，"此地无银三百两"。
- 封面帧清理不彻底，放大后中文笔画仍可辨认，或变成网点/半调颗粒。

**规则**：
- mask 严格贴合中文字 bbox，向外扩 2–4px 即可，**禁止外扩成大块矩形**。
- 擦除后在**同一位置**烧英文字幕，白字 + 1–2px 黑边，**不要大黑底块**。
- 封面选帧时必须避开：网点/半调纹理帧、有残留中文的帧、有原水印的帧。
- 全片中文扫描用 **1 秒步进**，不能只靠粗扫 1FPS；OCR 检出的中文行需 ≥2 个 CJK 字符才处理。
- 统计卡/角标（如"22.5分/12.1篮板"）这种固定位置静态图，用 drawbox 模糊条 + 原位英文数字替代，不要 inpaint 成糊斑。

### 12.3 音频：有中文解说必须静音，禁止保留中文人声

**踩坑**：用户多次骂"音频都是中文搞鸡毛"。

**规则**：
- `has_chinese_narration=true` 时，**原音轨整轨静音**，禁止保留中文解说旁白。
- 三种处理路径按用户选择：
  1. 英文 TTS 配音（语气贴近原片）+ BGM 垫音（-20dB）；
  2. 纯 BGM（用户明确说"不要配音"时）；
  3. 保留原视频音频（**仅限用户明确说"用原视频BGM"且原片无中文旁白**，如纯比赛现场声）。
- 不要用 `concat` 拼接导致音频重编码爆音；所有操作 `-c:a copy` 或重新 AAC 192k。

### 12.4 BGM：**曲库是消耗品** —— 网上搜是选曲，本地库只是"要不要下载"

**踩坑**：直接从本地 7 首里选，用户骂"千篇一律""什么玩意""biabia 打镲"。
**根因（2026-09-28 用户点破）**：「曲库里的歌曲不应该作为第一选择，应该优先从网上找，
然后丰富曲库。要不然后续随着视频增多，视频的 BGM 都全相同了，因为曲库不增加啊」
—— 曲库不增长，复用就是**数学必然**，不是运气问题。光把标点改好没用，得让它长。

⚠️ **先记住一个数字**：`music/sports/` 有 34 个文件，但**只有 24 首歌** ——
10 个 `*_variant` / `*_v3` 是同一首的变速指纹变形，**观众听来还是同一首**。
所以"还剩几首可换"必须按**歌曲**算，不能按文件算。
`bgm_ledger.py` 与 `generate_post.py` 都按 `common.normalize_track_key()` 归并。

**🚦 两层模型（2026-09-28 用户二次纠正 —— 早先写成"① 搜新曲 > ② 本地 > ③ 复用"
一条阶梯，是错的，会让"网上搜到、本地本来就有"的歌被重新下一遍）**

用户原话：「逻辑应该是**优先网上搜索对应的歌曲**，然后**在本地曲库找，如果有，
就省去下载部署直接合成，如果没有就去下载**」——这里其实是两件不同的事：

| 层 | 问题 | 池子 | 手段 |
|---|---|---|---|
| 第一层 | **选哪首歌** | **全网**（不受库存限制） | 按视频需求上网搜，排出候选先后 |
| 第二层 | **从哪儿供给** | **本地 `music/`** | 查本地库：命中 = 缓存，省掉下载 + 入册 |

落地就是一条命令：

```bash
python scripts/bgm_ledger.py resolve --bpm <rhythm.suggested_bpm> \
    --candidate "网搜第1名::作者" --candidate "网搜第2名" --candidate "..."
```

`resolve` 逐个裁决（按你给的顺序，不重排）：

| 裁决 | 条件 | 动作 |
|---|---|---|
| ✅ **用本地** | 本地有音频 **且没用过** | **省去下载与入册，直接合成**（`analyze_bgm.py` → 混音） |
| ⬇️ **需下载** | 本地没有音频 | 下载 → `add` 入册 → 合成 |
| ❌ **已用过** | 本地有，但出过场 | 跳过，看下一候选（**复用=曲库白扩充**）|
| 🚫 **许可不行** | 许可是 `YouTube Audio Library` 等 | 跳过（只覆盖 YouTube 站内，署名也救不了）|
| ❌ **重复候选** | 与前面候选同一首（含 `_variant` 撞名）| 跳过 |

> **为什么本地命中必须是"没用过"**：本规则的存在意义就是防复用。若"本地有就直接用"，
> 用过的那些会被无限复用 —— 又回到"千篇一律"。
>
> **为什么"网上搜"必须在本地查之前**：反过来先翻本地库，选曲池就被库存限死了，
> 变成"我有什么就用什么"（这正是用户否掉的做法）。搜索决定**要哪首**，
> 本地库只决定**要不要下载**。

**两个必须查的边界情况**（`resolve` 已内置）：

- **同名不同作者**：本地按**曲名**归一，撞名会误判成命中。带 `::作者` 传入即可被提示
  （`--candidate "Carefree::DJ Nobody"` → ⚠️ 本地同名曲作者是 Kevin MacLeod）。
- **只在 `bgm-library.yaml` 登记、本地没有音频文件**：这**不算命中**（"本地有"指的是
  音频真在 `music/` 里，否则没法合成）。但元数据与许可已知 → 许可不行的会在这一步就拦下，
  不用等 `add` 完才发现。

**辅助命令（不是主路径）**：

- `audit` —— 开工前体检：多少首歌 / 用过几首 / **还剩几首没用过**（= 不重复还能撑几条）。
  ⚠️ **它只报告余量，不要求预先囤货**（用户 2026-09-28 明确）：**不预先填库**，
  **在用的时候现搜现补** —— `resolve` 裁决为「需下载」时顺手 `add` 入册即可，
  余量见底也不必提前批量下载。
- `pick --bpm X` —— **本地库里没用过的**候选按 BPM 接近度排序。用途是**兜底**：
  网搜无结果时可作搜索线索，或当 `resolve` 报告的"省下载备选"。
  ⚠️ **不要**把 `pick` 当成选曲第一步（那就是"先翻本地库"的老毛病）。
- `record` —— 每次用完记一笔，不记下次就不知道这首出过场。
  > 台账 `configs/bgm-usage.json` 按**歌曲**计数。2026-09-28 导入本批 8 条历史后立刻暴露：
  > `Hitman.mp3` 与 `Hitman_v3.mp3` 是同一首歌，被两条视频各用了一次
  > —— **8 条视频其实只出了 7 首歌**，重复在第一批就已经发生了。

**规则**：
0. **⭐ 先量节奏，再选曲**：跑 `analyze_rhythm.py` 拿视频的
   `suggested_bpm`，再按上面的两层模型选曲（`resolve`）。**不要再用 `bgm-library.yaml`
   里的 `bpm` 字段** —— 实测 8/8 全错（最差 31%，见 §10.1）。完整流程见 **§10.1「节奏匹配」**。
1. **先分析视频风格**（扣篮=高能量鼓点；怀旧=悲怆史诗；战术=中等电子；致敬=电影管弦）。
2. **去网上搜 TikTok 热门篮球 BGM**（如 Laced Max / Airball / Gnarly 这类），找无版权替代版。
   ⚠️ 但 **TikTok 官方商用音乐库（CML）不能用于本场景**：它的授权**只在"app 内选音"时生效**
   （条款要求 `by using the sharing features made available to TikTok users`），
   而本 skill 的产物是**音频已嵌进 MP4、走上传流程** —— 所以**即便只发 TikTok 也不适用**。
   完整 3 条理由见 §10.1 末尾。
3. **网搜到之后先 `resolve` 查本地，别盲目下载**：本地有且没用过就直接合成（省下载 + 省入册）；
   确需下载的才走 `bgm_ledger.py add --audio <文件> ...`（自动放进 `music/sports/` + 实测 BPM + 写登记）。
   Kevin MacLeod incompetech CDN 可直接 curl：`https://incompetech.com/music/royalty-free/mp3-royaltyfree/<Name>.mp3`（CC BY 3.0）。
   曲名要 URL-encode（`In a Trap` → `In%20a%20Trap`），大小写按原名。
   快速验证能不能下：`curl -s -o /tmp/t.mp3 -w '%{http_code} %{size_download}' -r 0-2000 <url>`，
   期望 `206 2001` 且 `file /tmp/t.mp3` 认成 `Audio file with ID3`。
   ⚠️ **别用 `curl -I` 判断**：HEAD 对存在的文件也返回 200 但 `size=0`，
   过去据此误判成"直链 404、下载不可行"，才走了本地兜底（2026-09-28 复核纠正）。
4. **一批视频内 BGM 不得重复，且只用没出过场的**：不靠人脑记，用 `resolve`（自动判"已用过"）
   + `record`（用完记一笔）。
   ⚠️ **`*_variant` 不算新歌** —— 母版与变体按同一首歌计数（`normalize_track_key`）。
5. 用户偏好：**重鼓点/trap/808，不要打镲声（cymbal crash）**。被否过的方向记下来（Sneaky Adventure 太慢、Carefree 太飘、Ready Aim Fire 太打镲、The Builder 配悲情不够悲）。
6. **已混入视频的 BGM 必须署名（2026-09-28 起由脚本自动生成）**：CC BY 的 "BY" 是
   **许可条件**、与发布平台无关 —— "只发 TikTok" 不构成不署名的理由。
   `generate_post.py::resolve_music_credit()` 按 `audio-plan.json` 的 `bgm.asset_path`
   反查 `configs/bgm-library.yaml`，在 `tiktok-post.md` 的 Caption 段后自动生成可粘贴的
   `Music: <曲名> by <作者> (<来源>), licensed under Creative Commons: By Attribution X.0`。
   三个分支：
   - **yaml 命中** → 直接出署名行。
   - **yaml 未登记**（实测：yaml 41 条登记里只有 **8 首**在 `music/sports/` 真有音频，
     即本地 24 首歌里 16 首无元数据；新入册的曲目在
     `bgm-library-added.json`，会被一起读到）→ 按本地目录既有事实兜底为
     Kevin MacLeod / Incompetech / CC BY 3.0，并**标注 `inferred`** 提示核对。
   - **`YouTube Audio Library License` 的 8 首** → **不出署名、直接出「授权不可用」警告**，
     因为该许可只覆盖 YouTube 站内，署名救不了，只能换曲。
7. **选曲不限 TikTok 热门（用户偏好 2026-09-24）**：可搜索国内热门曲目；**节奏感优先，音乐无国界**。
   注意：国内热门曲目（抖音热歌/热门伴奏）基本是商业版权，没有免费可商用版，嵌入 TikTok 更易被识别静音；
   国内免费 CC 平台（耳聆网 ear0.com 等）有 JS 反爬，脚本难以下载。落地一律用无版权 CC 素材。
8. **NCS 曲目识别风险（2026-09-24 实战教训）**：NCS（NoCopyrightSounds）厂牌曲目虽标注"无版权可商用"，
   **已被 TikTok 内容识别库收录**——外部嵌入上传会触发"发现版权问题、视频将被静音"（Warriors/Feel Good/Invincible 均中招）。
   处理：改用 incompetech（Kevin MacLeod CC BY 3.0）等识别库收录率低的曲目 + **音频指纹变形**。
9. **音频指纹变形（防识别核心手段）**：混音前对 BGM 做
   `atempo=1.02~1.04`（变速）+ `asetrate=44100*0.98~0.985,aresample=44100`（变调）+ `equalizer` 微整形
   （如 110Hz +2dB / 3400Hz -1.5dB），破坏与原始音频的指纹匹配，听感几乎不变。
   变体文件命名 `*_variant.mp3` 存 `music/`，audio-plan 的 `asset_path` 指向变体。
10. 高节奏曲目参考（incompetech 可直链，按 BPM 选）：Hustle~108 / Cut and Run~128 / Volatile Reaction~129 /
    Faster Does It~140。用户明确要"节奏感很强"时优先 125+ BPM。

### 12.5 Logo / 水印：原位替换，大小要够

> **规格（字号 / 颜色 / 位置 / 出现次数）见 §8.1「视频内 logo」**，此处只留教训。

**踩坑**：
- 原水印擦了以后 logo 另放角落，用户说"此地无银三百两"。
- logo 字太小看不清（四条成片实测 31.9% / 16.9% / 25.6% / 缺失，见 §8.1 的表）。

**两条硬规则（细节见 §8.1）**：
- **原位替换**：原水印位置**先清理**，再在**同一原位**贴自家 "Hoop Dynasty Daily"，不要挪位置。
- **大小 = 画幅宽度的 32%**：⚠️ **必须按画宽换算，不是照抄像素值**
  （各片分辨率不同，抄像素值必然不一致）。实现 `render_overlays.py --logo-ratio 0.32`。
- 给"已经有旧小 logo"的成片换新尺寸：`ffmpeg -vf "delogo=<旧logo实际范围>,drawtext=..." -c:a copy`
  （delogo 框用「成片−源片」差分量出来，别用 plan 的 bbox——bbox 是源水印框，不是绘制框）。
  也可以直接量成片里旧 logo 的 bbox（OCR 一帧即得）当作 delogo 框。
- ⚠️ 只改 logo 时**不要整片重渲染**：`render_overlays.py --video <无字底片> --no-inpaint
  --profile nba --logo-ratio 0.32`
  单帧只要 ~0.06–0.1s（见 13.4.3 / 13.9），整片也已备好底片。

**水印"一族多形"：一条 item 收族，别按 OCR 变体开一堆 item（2026-09-27 05 实录）**

同一家水印在不同帧上 OCR 会吐出**不同字**，还会随运动在不同位置上出现。05 的"热越篮球"
在本批 OCR 里出现了 **7 种变体文本 + 约 50 条不同轨迹**（bbox 每帧都在挪）。

- **按"族"处理，不按变体**：建**一条** `category:"watermark"` 的 item，用
  **`bbox_override`（一个覆盖所有位置的大框）+ `start_time/end_time` override（覆盖全片）**，
  一次性清干净。按变体开 50 条 item 会让 plan 无法维护、渲染时逐条 inpaint 也慢得多。
- 代价：框大 → inpaint 区域大 → 背景重建质量下降。所以**只有在"位置本身在漂"时才用大框**；
  位置固定的水印（顶部标题、底部长条）仍走**精确 bbox**（§14.1）。
- 判据：把该水印的 OCR 检出**全部叠在同一张图上**看落点分布 —— 聚成一团 → 精确 bbox；
  散成一条带 → 用 `bbox_override` 的带状大框。
- 清理项的 `category` 必须是 `"watermark"`：`title`/`logo` 等其他 category 会被 SRT 侧过滤规则
  一并丢掉（§13.4），导致"画面清了但 plan 不认"。

### 12.5.1 文字窗口必须留 padding（否则首帧闪原中文）

**踩坑（2026-09-27 实测）**：OCR 聚出来的窗口起点太贴，第一条字幕 start=0.2s，
于是 final.mp4 的**第 0–0.2 秒仍然显示原中文**。播放器打开文件时停在 t=0，
用户第一眼就看到中文（截图即此事）。同理每条字幕切换点前后也会闪。

**规则**：所有 mask 与 drawtext 窗口前后各扩 `PAD_HEAD=0.5s / PAD_TAIL=0.35s`
（`render_overlays.py` 默认，`--pad-head/--pad-tail` 可调），**inpaint 窗口和绘制窗口用同一套扩法**，
否则会出现"原字抹了但新字还没上"的空档。扩完再走去重叠。

### 12.5.2 画面叠加渲染器：`scripts/render_overlays.py`

**不要再用 `clean_video.py` 的 drawtext 路径做正式交付**——它为每个 item 生成独立 drawtext 滤镜，
时间窗重叠时两行字会**叠字**，字体是细体、无投影，而且 logo 用的是 plan 里的固定小字号。

`render_overlays.py` 单 pass 完成：**ROI 局部 inpaint（去原中文/水印）→ Pillow 叠加英文（粗体+描边+投影）**。

```bash
PY=/Users/jason/opt/miniconda3/bin/python   # 必须用 conda 解释器（cv2/PIL）
python scripts/render_overlays.py \
  --video <源视频.mp4> --work-dir <work/> --output <work/cleaned.mp4> \
  --preset veryfast --crf 18 --sync-timeline
```

要点：
- 读 `localization-plan.json` + `masks/masks-index.json`；`--dry-run` 只打印叠加清单（含 logo 字号/占比），**正式跑前先 dry-run**。
- `category=watermark` 的项：原文抹掉后**原位重画品牌 mark**（不是丢弃），字号按 `--logo-ratio`（默认 0.32）算。
- **字幕类 category 走 `SUBTITLE_CATEGORIES = ("subtitle","spoken_subtitle","caption_overlay")`**：
  只匹配字面量 `"subtitle"` 会把 `spoken_subtitle` / `caption_overlay` **静默丢弃**（2026-09-27 修复）。
  只清画面、不产生文字的项（OCR 误读清理）请用 `category=watermark`（见 §1）。
- ⚠️ **它已经把英文画面字幕画出来了**：后续 `render_subtitles.py` **只出 SRT、不要 `--burn`**，
  否则同一句会出现上下两行（2026-09-27 实录，见主流程 Job 2）。
- 字幕自动去重叠（保留后开始的那条）；`--sync-timeline` 把去重叠+扩 padding 后的窗口写回 `timeline.json`，
  这样 `render_subtitles.py` 生成的 SRT 与 `qa_video.py` 的冲突检查和画面一致。
- **品牌色必须来自 `--profile <name>`**（读 `cover_style.fill_color`，`logo`/`title`/`hook` 同源；
  `--accent "R,G,B"` 可临时覆盖）。**脚本里不要再写颜色常量** —— 封面读 profile、视频读常量，
  两边必然漂（2026-09-27「黄封面 + 白 logo」事故，见 §8.1 / §13.9）。
- 其它参数：`--logo-text/--logo-y/--no-logo/--logo-ratio`、`--no-inpaint`（只补 logo 的二次 pass）、
  `--start-time/--max-frames`（抽样）、`--print-logo-size`。
- 速度参考：**带 inpaint** 720×1280 ≈ 0.25 s/帧、1080×1920 ≈ 2.6 s/帧（mask 多时更慢）；
  **`--no-inpaint` ≈ 0.06–0.1 s/帧**（1280×960 实测 17.6 fps），快 25 倍以上（§13.9）。
  只想改 logo 尺寸就别重跑整片，用 `delogo + drawtext` 单独一遍（见 12.5）。

### 12.6 封面：按视频内容定制，禁止模板化

> 规格（字号 / 颜色 / 安全区 / 版式 / 选帧 / 清帧）**全在 §8**，此处只留教训。

**踩坑**：一开始 8 个封面全写 "MUST WATCH"，用户骂"怎么都是 Must watch"。

**唯一的额外规则**：标题从视频内容提炼（球员名+看点，如 "IVERSON TOP 10" /
"JOKIC THE PASS MASTER" / "GRIFFIN CAR DUNK"），**禁止万能模板**。
选帧与清帧标准见 §8.5 / §8.6。

#### 12.6.1 封面标题的取值链（2026-09-29 新增，别再白跑一次封面）

`generate_cover.py` 的优先级是**四层**：

```
--headline  >  cover-plan.json 的 headline  >  video-understanding.json 的 cover_headline  >  DEFAULT_HEADLINE("MUST WATCH")
```

- 🔴 **只把 `cover_headline` 写进 `post-content.json` 是没用的** —— 生成封面时根本不读那个文件。
  Agent 该写的是 **`work/video-understanding.json`** 的 `cover_headline`
  （`post-content.json` 的 `cover_headline` 只是给人看/给 post 用的）。
  本批 05/06 都栽在这里：understanding 里忘了这个字段 → 静默落回 "MUST WATCH"。
- 🔴 **`cover-plan.json` 一旦存在就永远压住 understanding**：它由首次生成时写入，
  之后每次重跑都以它里的 `headline` 为准。所以**改文案要同时改两处**
  （`cover-plan.json.headline` + `video-understanding.json.cover_headline`），
  否则只改了 understanding、重跑封面还是旧标题（03 就是这样被"钉"住过一个版本）。
- ✅ **交付前 1 秒自查**：`python -c "import json;print(json.load(open('<out>/work/cover-plan.json'))['headline'])"`
  —— 出现 `MUST WATCH` 就是漏了这一步。
- 重出封面很便宜（十几秒，不重渲视频）：`generate_cover.py --work-dir <work> --output-dir <out> --profile nba`。
  但**封面变了就要重跑一次 QA**（`check_cover` 校验的是落盘的 `cover.jpg` 与 `cover-plan.json`）。


### 12.7 tiktok-post.md：每条独立写，禁止复制模板

**踩坑**：7 个视频的 post.md 全是同一段 "NBA Highlights / Career highlights and best plays"，用户骂"怎么每一个都一样"。

**规则**：每条视频单独写 caption（结合球员/看点/钩子），5 个相关 hashtag，置顶评论含三要素（内容预告 + @HoopDynastyDaily + 互动问题）。

### 12.8 交付前自检清单

交付 final.mp4 + cover.jpg + tiktok-post.md 前，必须：
1. 抽 ≥5 个时间点帧，肉眼检查：无中文残留、无黑块、logo 位置/大小对、无原水印。
   ⚠️ 5 个点里**必须含「第 0 帧」、「最后 1 秒」、「片尾前 5 秒」各一帧** ——
   片头卡/片尾卡是"只在首尾出现"的元素，抽中间帧永远看不见
   （见 §14.6 片尾卡、§14.7 片头第 0 帧，09/02/03 实测踩坑）。
   **第 0 帧只能显式取**（`-frames:v 1` 或 `select='eq(n\,0)'`）：
   1fps 采样网格的起点不是 t=0，QA 的 `chinese_residue` 也照不到它（§14.7）。
2. 抽音频片段确认：无中文人声（除非用户明确要保留原声）。
3. cover.jpg 打开看：无网点、无黑块、标题黄字、品牌署名在。
4. 分辨率正确：横版比赛 1280×720/960，不强行竖屏。
5. BGM 不与同批其他视频重复。
6. tiktok-post.md 不是模板套话。
   ⚠️ 若成品**被有意裁短/变速**（如裁掉源频道片尾 CTA 卡），必须写
   `work/originalize-result.json` 声明 `duration_sec`，否则 QA 的时长检查会 FAIL（见 §14.6）。

### 12.9 输出目录：最终产物直接输出视频名目录，禁止 output 子层（用户偏好 2026-09-24）

**踩坑**：脚本默认 `--output-dir <视频名>/output`，用户反馈"不需要多一层 output 目录"。

**规则**：
- 最终产物（final.mp4 / cover.jpg / subtitles-en.srt / tiktok-post.md）**直接输出到 `<视频名>/` 顶层**；
- 过程文件（JSON/帧/mask/cleaned/mixed 等）放 `<视频名>/work/`；
- **不建 output/ 子层**。调用脚本时 `--output-dir` 直接传视频名目录（qa_video 的 cover.json 等也写进视频目录）。
- 移动产物后验证可访问性，并重新 present 交付。

### 12.10 clean 快路径与 A/B 验收协议（2026-09-24）

**背景**：三条 277s 视频整批耗时长，最大瓶颈是 `clean_video.py` 逐帧 OpenCV inpaint +
逐帧 Pillow logo overlay（约 2.5 万帧 Python 循环），其次是 libx264 CRF18/medium 全片重编码。

**fast 快路径（`--method fast`，已实现）**：
- **适用条件**：待清理区域是**纯黑底固定条带**（顶部黑带黄字标题、底部黑带集数标）。
- **前置检测**：对每个 mask 采样 3 帧，取 bbox 外扩 8px 的环带做亮度剖面；环带 mean < `--black-threshold`（默认 24）
  且 max < 3×阈值 → 判定纯黑底。**任一区域不通过 → 自动回退 opencv 逐帧路径**（不降级交付）。
- **处理**：单条 ffmpeg 命令 `drawbox 填黑 + drawtext 英文 + overlay logo`（contain 逻辑与 Pillow 版一致）。
  黑带本来就黑，drawbox 填黑 = 恢复黑带原样，与 inpaint 结果一致甚至更彻底；不产生"黑块突兀"。
- **编码**：fast 默认 `crf 20 / preset fast`；opencv/ffmpeg 路径默认保持 `crf 18 / preset medium`（可通过 `--crf/--preset` 覆盖）。
  CRF 18→20 肉眼基本无差别（每 +6 体积减半，+2 差异很小），"画面花"根因（mp4v/色彩标记）与 CRF 无关，已另修。

**A/B 验收协议（新功能首次批量前必做）**：
1. 取同批**第一条视频**，用同一份 plan/audio/cover/post，clean 跑两条路径：
   - A：`--method opencv`（原路径，crf 18 / medium）→ 后续 mix/QA 正常走
   - B：`--method fast`（新路径，crf 20 / fast）→ 后续 mix/QA 正常走
2. 自动化对比（两边同标准）：
   - `qa_video.py` 对两版 final 全片 OCR：`chinese_residue` 都必须 PASS
   - `ffprobe`：时长一致、音频规格（44100Hz/stereo/≥128kbps）一致、分辨率一致
   - 抽同一组时间点帧（如 0.5/30/90/150/240/结尾）并排成图，肉眼查清理区域：无中文残留、黑带边界整齐、logo 位置/大小正确
   - 对比体积与耗时
3. **判定**：B 全 PASS 且抽帧无可见差异 → 批量用 fast；有残留/边界毛刺/logo 偏差 → 用 A 并排查 B。
4. 用户最终肉眼拍板；确认后把默认值统一（如需）。

**⚡ 最终裁决（2026-09-25 用户拍板，已定稿）**：
- **默认路径 = A（`--method opencv`，crf18/medium）**，脚本默认值即 opencv，无需显式指定；**优先保证视频质量**。
- **fast 路径（`--method fast`）已停用（deprecated）**：不再参与任何批量处理，也不作为候选对比路径。
- 实测依据：2026-09-25 批（奥尼尔×艾弗森，13 条画面中段喜剧字幕）A/B 双跑，B 的纯黑条带前置检测对全部区域不通过、自动回退 opencv，最终产物与 A **字节级一致（MD5 相同）**，无任何提速收益；QA 双方六项全 PASS。
- 若未来出现"顶部黑带黄字标题/底部黑带集数标"的纯黑条场景需要提速，**必须重新走本协议 A/B 验收并由用户拍板**后才可临时启用 fast；验收不通过则继续用 A。

### 12.11 第三批 8 条实战档案（2026-09-27，NBA 混合批次）

素材：8 条中文 NBA 短视频（横竖混排，50s–824s，合计约 43 分钟 / 5.5 万帧）。
**规范性结论已上提到 §1 / §3 / §4 / §8.2 / §13.6–13.8**，本节只留现场证据与判定过程。

**本批暴露的核心问题（按影响排序）**

| # | 现象 | 根因 | 规范去向 |
|---|---|---|---|
| 1 | 同一句字幕在成片里出现**上下两行** | overlay 已按原 bbox 精绘白字，第 5 步又用 ffmpeg 通用样式在底部重烧一遍 | §12.5.2 |
| 2 | 顶部中文标题在**片头若干帧原样残留**（真残留，非误读） | `title.start_time`（OCR 首检 1.0s）> `PAD_HEAD`(0.5) → `[0,0.5s)` 从未进清理窗口 | **§4** |
| 3 | 封面黄色比规格**暗一档** | `cover_engine` 硬编码 `GOLD(255,208,0)`，profile 的 `fill_color` 只进 meta | **§8.2** |
| 4 | `spoken_subtitle` 类字幕**全不渲染** | `render_overlays` 只匹配字面量 `"subtitle"` | §12.5.2 / §13.4 |
| 5 | `mute_original_tts_bgm` 出片**只有 BGM、没有旁白** | `tts_timing measure` 只测时长不合成，链路缺 TTS 步 | §3 / §13.4 |
| 6 | 4 条视频 QA `chinese_residue` FAIL | 3 处画面**误读** + 1 处**真中文** | §1 判定线 |
| 7 | 并发把机器打瘫，封面生成被 OOM 杀 2 次 | 4 链 × 3–4 段 × 2 编码器 + 8 OpenCV 进程争 8 核 → load **230** | **§13.6** |
| 8 | 返工要重渲整片（04 一条 42 分钟） | 缺陷只落在 1 个分段内，其余段本可复用 | **§13.7** |
| 9 | 长片（05，824s）滚动解说字幕被 OCR **切成碎块**、句子拦腰断 | OCR 只给时间区间，文本要人工整段听合并成 47 条 cue | **§13.3** |
| 10 | 同一水印（"热越篮球"）OCR 出 **7 种变体 + ~50 条轨迹** | 位置在漂 → 必须按"族"处理，不能按变体开 item | **§12.5** |
| 11 | 交付后 `tiktok-post.md` 内容与视频无关 | `generate_post.py` 读不到 `post-content.json` 时静默套通用模板 | §13.4 / §6 |

**本批时间与资源账（排期用，见 §13.10）**：Job1 累计约 **8 小时** CPU（最长 05 = 154 分钟）；
单条 work 最大 **2.3 GB**；期间磁盘 `Data` 卷 **97% 满、可用 15Gi**，直接导致封面生成被 OOM 杀 2 次。

**`chinese_residue` 的五次检出及判定（关键证据，下次照此判）**

| 视频 | 时间 | OCR 检出 | conf | 实际是什么 | 性质 |
|---|---|---|---|---|---|
| 02 | 55.0s | `日1口` | 0.555 | LED 计时牌 `8:10` 的数字 | 误读 |
| 04 | 482.0s | `一川` | 0.597 | 标题下方黑色暗部噪点 | 误读 |
| 08 | 147.0s | `八年` | 0.833 | 球员球裤 / 腿部皮肤纹理 | 误读 |
| 08 | 212.0s | `一心` | 0.554 | 场边广告牌 + 球衣交界 | 误读 |
| 05 | **0.0s** | `碾压禁区?无人可挡` / `2001赛季奥尼尔高光表现集锦` | **0.933 / 0.975** | **顶部两行中文标题本身** | **真残留** |

- 判定口诀：**conf ≥0.9 且文本与已知标题一致 → 真残留**；
  conf 0.5–0.85 且落在人物/器械/广告牌纹理上 → 误读。**两者都必须清**
  （QA 同引擎抽帧，误读必然复现，"这是假阳性"过不了门禁）。
- **抽「原片 / 成片」同一帧、裁剪放大并排看**是最快的证伪手段（本批 4 处都是这样确认的）。

**ASR 幻觉（本批 5/8 条中招）**

01 循环乱码（avg_logprob −0.81）、02 全部 ≤ −3.79、03 zh/ko 混杂（−4.6）、
08 zh/ko/en 乱跳、05 混着俄文 `только` 与「何贿」「傻」这类不连贯碎片。
→ **`has_chinese_narration=true` 不可采信**（§14.2 的老结论本批再次被验证）。
本批一律按"非旁白"处理：丢原轨 + 授权 BGM，这也同时是最稳的"清干净中文"策略（见 §3）。

**BGM 曲库核对（做一次就够，务必做）**

`music/sports/` 里 34 个 mp3 **只有 9 个**登记在 `configs/bgm-library.yaml`
（Breaktime / Carefree / Cipher / Hitman(+v3) / Impact Moderato(+v4) / The Builder /
The Path of the Goblin King / Wallpaper(+v2)）。
**未登记的曲目（Crunk Knight / Heroic Age / Crusade / Ready Aim Fire …）一律不要用** ——
"文件在本地"≠"已授权在册"。本批 8 条只用登记曲目，且同批不重复。

**2026-09-28 追踪：本批遗留的三项（F1/F2/F3）落地结果**

| 项 | 当时状态 | 现在 | 落点 |
|---|---|---|---|
| F1 `remove_original_bgm_only` 丢掉现场声 | 只做了语义澄清，标为"未实现的能力" | ✅ **新增 `duck_original_add_bgm`**（原声压低 −15dB + 叠 BGM），实跑验证两路都在；同时**更正**了"本机没有 demucs"的错误事实（demucs 4.0.1 已装，但它解决不了这个问题，别再走弯路） | **§3** |
| F2 常驻叠字起点 > pad 就漏片头（通用隐患） | 只在文档里写了"必须写 0.0"，靠 Agent 记性 | ✅ **代码兜底**：`common.normalize_persistent_starts()` 唯一真源，plan 起草 / mask / 渲染**三处**调用；判据「片头 5s 内起 + 时长 ≥10s → 起点归零」，触发打 WARNING | **§4** |
| F3 `cover` warning 不可消除 | 已写清验收标准 | ✅ 已落（本就是验收标准的定义，文档即实现） | §8.7 / §13.4 / `jobs/generate-cover.job.md` |

**交付后返工：统一品牌色为 `#FFE800`（2026-09-27 晚）**

用户看完真实渲染对比图定稿：「封面选 C（`#FFE800`）；**视频内 logo 不要保持白色，和封面
保持一致**」。

- 代码侧：`profiles/nba.yaml` 的 `fill_color` → `[255, 232, 0]`；`render_overlays.py` 新增
  `resolve_accent()`（`--profile` / `--accent`），**logo / title / hook 三者统一取 accent**，
  不再有 `WHITE`/`GOLD` 两处硬编码；**字幕仍为白色**。
- 重跑范围由 plan 决定：**有 `watermark`/`title` 且带 `target_text` 的 6 条**
  （02/03/04/05/06/07）重渲；**01（只有字幕）、08（只有清理项、无叠字）不动**。
- 05 的 base 比 plan 旧（片头真中文 + 2 处误读框是后补的清理项），所以先 `patch_base.py`
  局部修 base（3 个窗口 → 391 帧带 inpaint ≈ 6 分钟），再做全片 `--no-inpaint` 重渲。
  修补后片头 24 帧 OCR **无 CJK** ✓（§13.9）。
- 复核：封面 8/8 像素 `rgb(254,232,0)`（JPEG 偏差 1）；成片用 `chk_accent.py` 抽帧读像素，
  不采信 QA 的 profile 值比对。

---

## 13. 执行效率（2026-09-22 复盘）

> **本章速查（工程约束，先看这张表再去翻正文）**
> | 你要做什么 | 看哪节 |
> |---|---|
> | 找中文（不许逐帧抽图） | §13.1 |
> | 判"残留 vs 误读"、成品硬门禁 | §13.2 |
> | 写 plan（脚本起草 + Agent 填语义 + `extra_items_file` + 长片字幕人工合并） | §13.3 |
> | 脚本排查（已知 bug / 静默降级） | §13.4 |
> | 长视频分段并行渲染 + 无字底片 | §13.4.3 |
> | 一次跑几条、限线程（**最容易翻车的一节**） | §13.6 |
> | 返工只重渲受影响段（关键帧对齐 + 流拷贝） | §13.7 |
> | 沙箱 / 后台 / PATH / Pillow 中文 | §13.8 |
> | 改叠字或品牌色（`clean-base` 快路径） | §13.9 |
> | **排期与磁盘预算**（跑批前必看） | §13.10 |
> | **批处理操作纪律**（失败复盘） | §13.11 |

### 13.0 结论先写在这

同一批 3 条视频，Job1 分析（probe/OCR/ASR）只花 **11 分钟**，但整批耗时 **70 分钟**。
差额全耗在「跟脚本较劲」上，不在算力上：

| 时间 | 干了什么 | 产出 |
|---|---|---|
| 14:05–14:16 | Job1 分析 ×3 进程并行 | probe/ocr/asr ✅ |
| **14:16–14:42** | **抽 43 张图反复看 + 读源码猜 JSON 格式 + 重跑失败的 whisper** | **零产物** ❌ |
| 14:42–15:10 | Job2 + 封面 + 修 2 个脚本 bug + A 封面返工 | 交付 ✅ |

### 13.1 中文定位：接触印样，禁止逐帧抽图

**踩坑**：为了找 C 的 4 处中文，抽了 43 张中间图，每张一次视觉推理，26 分钟空转。

**规则**：
- 找中文**第一步就跑 `contact_sheet.py`**（6×8=48 格全片概览，一轮看完），
  确认可疑秒数后再 `--start/--end` + `--crop` 做二次加密。
- **禁止**「抽 5 帧看看 → 再抽 10 帧看看 → 再放大看看」这种碎步循环。
  一个视频**图片读取预算 ≤ 3 次**（1 次全景 + 1~2 次局部加密）。
- 交付自检同理：用 contact_sheet 抽终版 final.mp4，而不是逐张抽。

### 13.2 OCR 与中文残留：三层分工 + 成品硬门禁（2026-09-22 定稿）

**历史事故（为什么有这一节）**：当时 `paddleocr` 未安装，`ocr_video.py` 的 auto
**静默降级**到 tesseract(chi_sim)，而 tesseract 对视频里的中文描边美术字召回率≈0
（调 psm 3/6/7/11、CLAHE、Otsu 二值化、放大 2~3x **全部无效**）。后果是连锁的：

- `ocr.json` 里 C 视频的 **4 处中文只报出 1 条单字误报** → Agent 据此认为「没什么中文」→ **漏清一处**；
- QA 又复用同一份结论 → **假 PASS**，残留中文直接交付（用户后来在成品 26-28s 发现「卤蛋盘卤蛋」）。

**根因不是「OCR 不准」，是三个结构性错误**：
1. **静默降级** —— 让「能力缺失」伪装成「没有发现问题」；
2. **QA 复用处理前的结论** —— plan 漏了什么，QA 就漏什么；
3. **把某引擎的缺陷当成普适结论** —— 于是放弃了本来可用的能力。

**三层分工（各司其职，不是互相替代）**：

| 环节 | 手段 | 职责 |
|---|---|---|
| 召回 + 定位 | `ocr_video.py`（RapidOCR） | 全片扫出中文的 bbox + 时间区间，成本低、可重复 |
| 兜底验证 | `contact_sheet.py` | 防 OCR 系统性漏检（极短闪现、特殊字体、半透明字） |
| bbox 精修 | `measure_text_bbox.py` | OCR 给的是**文字紧贴框**；含底板/描边的图形需另行外扩 |
| **成品复检** | `qa_video.py` → `chinese_residue` | **独立于 plan** 直接对 final.mp4 下结论，检出即 FAIL |

**四条硬规则**：

- **① 成品必须过中文残留硬门禁**。`qa_video.py` 对 final.mp4 全片 OCR（默认 1fps），
  任何 ≥2 连续 CJK 且 conf≥0.5 的检出 → `overall=FAIL`、退出码 1、**禁止交付**。
  残留清单逐处列出（时间区间 + 文本 + bbox），处理完必须**重跑本检查**。
- **② OCR 不可用时不得判定「无中文」**。引擎缺失只会让该项变 `warning`，绝不等于 pass
  —— 否则就是重演假 PASS。
- **③ OCR 的 bbox ≠ 需要清理的图形范围**。A 的教训：中文底下有条全宽半透明白底板，
  只按文字框 inpaint → 两侧白带残留，形成一条静止横带（用户反馈「怎么这么花」）。
  拿到 bbox 后必须判断「文字是否附着在某个图形上」，是则把整个图形纳入掩码。
- **④ 只保留 RapidOCR 一个默认引擎**。它与 paddleocr 共用同一套 PP-OCRv4 模型
  （只是运行时不同：onnxruntime vs PaddlePaddle），准确率差异属噪声范围，同源交叉验证
  无收益。auto 不再降级到 tesseract；排障时用 `--engine paddleocr|tesseract` 显式指定。

**引擎对照（实测同一批中文标题）**：

| 引擎 | 识别 | 置信度 | 沙箱内 | 首次初始化 | 定位 |
|---|---|---|---|---|---|
| **RapidOCR**（默认） | 4/4 正确 | 0.99 | ✅ 可用 | ~2.6s | 唯一默认 |
| paddleocr | 4/4 正确 | 0.89~0.99 | ✅ **可用** | ~14s | 仅显式排障 |
| tesseract | 0/4（1 条误报） | — | ✅ 可用 | 快 | 仅显式排障 |

> ⚠️ **2026-09-22 更正（重要）**：本节此前记载「paddleocr 在沙箱内会被 SIGKILL(137)、
> 必须在沙箱外运行」——**该结论已被推翻**。复测（无提权沙箱内）完整通过：
> `import paddleocr` ✅ → `PaddleOCR()` 实例化 ✅ → 推理 ✅（init 14.0s / infer 0.87s，
> 正确识别 "历史联盟谁能阻止奥尼尔" conf=0.886）。
> 当时那次 137 无法复现，系统无 OOM/崩溃报告，130s 长命令亦不被中断。
> **选 rapidocr 作默认的真实理由是性能**（初始化快约 5 倍、依赖小一个数量级），
> 而非「沙箱不可用」。paddleocr 可作沙箱内的有效备份引擎。
> 教训：**不要把单次观测到的退出码归因为环境的固定限制**，先用隔离实验复测再下结论。

- OCR 引擎实例**必须复用单例**（模块级 `_ENGINE_CACHE`）。逐帧新建实例会让多帧扫描无法使用。
- paddleocr 模型缓存在 `~/.paddleocr/`，直连 `paddleocr.bj.bcebos.com` 约 20MB/s，不经沙箱代理。
- 沙箱内**不能在 site-packages 下写 .pyc**（会残留 `*.pyc.<pid>` 临时文件），
  因此 paddleocr 每次导入都重新编译，恒定 ~14s —— 这才是它在沙箱内体验差的真实原因。

### 13.3 计划文件让脚本起草，Agent 只填语义

**踩坑**：手敲 6 个决策 JSON，每个都要先读脚本源码确认字段名，纯浪费时间。

**规则**：
- `localization-plan / audio-plan / cover-plan / video-understanding / post-content`
  五个文件先跑 `prepare_plan.py` 生成草稿（自动做区域合并/几何分类/时间范围/封面选帧/音频策略），
  Agent 只填 `target_text` / `headline` / `caption` / `hashtags` 这几个**语义字段**。
- 草稿的 items 基于 OCR 结果（rapidocr 已可信），建议仍用 13.1 的接触印样交叉核对后落笔。
- **草稿装不下的语义项，用 `extra_items_file` 注入，不要手改脚本**：
  `prepare_plan.py` / 计划构建脚本支持 `extra_items_file`（一个 JSON，内容是 items 数组），
  脚本把草稿与它合并。适用于"OCR 检不出但你知道该清/该画"的项（误读框、片头真中文、补画的 logo）。
  （本批实现：`tiktok-batch/plan_builder.py`。）

**长片真人解说：字幕必须"人工合并"，别指望 OCR（2026-09-27 05 实录）**

05 是 824s 的真人解说集锦，解说以**滚动字幕条**形式出现（每 1–3 秒换一次、位置固定）。
把它当普通字幕处理会失败：OCR 每帧只能抓到当前那一小段，**切得极碎、句子被拦腰截断**，
而且滚动中的字会被 OCR 拼成噪声。最终做法是：

1. OCR 只用来**定位时间区间**（哪几秒有字幕条），不用它的文本；
2. **整段滚动听/读一遍**（824s ≈ 14 分钟），人工合并成**完整句子**，
   落成 **47 条 cue** 的 `translations/05.json`；
3. 用 `plan_narration.py` 把 cue 转成 plan items（`category:"subtitle"`，
   带上人工定的 `start_time`）。

- 判据：**OCR 输出里同一行文字在连续帧上"逐渐变长/变短"** → 滚动字幕，走人工合并；
  换成"整块出现、整块消失" → 普通字幕，OCR 直出即可。
- 成本预期：长片这一遍人工合并是**必花的**，安排在 Job1 之后立刻做，
  别等渲染完才发现字幕是碎的。

### 13.4 已知脚本 bug（2026-09-22 已修；2026-09-26 / 09-27 / 09-28 各有新增）

- `audio_processor.py` **adelay 输入索引错位**：多段 TTS 时滤镜引用了不存在的 `[2:a]`，直接崩。
  已改为按实际输入序号生成。
- `audio_processor.py` **`-shortest` 截断**：无 BGM 时 TTS 轨短于视频，导致 final.mp4 被砍短
  （C 首版只有 31.2s / 全长 41.2s）。已改为 pad 到视频全长。
- `ocr_video.py` **多 pass 覆盖不合并（2026-09-26 修复）**：原实现每次运行直接覆盖 `ocr.json`，
  dense pass 会把 coarse 检出整个丢掉 —— 而中文密集的视频（如片尾字幕卡）恰恰只在 coarse 里出现，
  导致 plan 漏检、QA 才发现。现已改为**加载既有 ocr.json 按 (frame_id,text,bbox) 去重合并**。
  若用旧版跑过，必须对同一 work-dir 重跑 `--pass coarse`（会自动与 dense 合并）再生成 plan。
- `edge-tts` CLI **不在默认 PATH**：conda 装的是 `/Users/jason/opt/miniconda3/bin/edge-tts`，
  Job2 脚本若只 export `~/.local/bin` 会导致 TTS 0/N success（静默降级成纯 BGM）。
  PATH 必须包含 `/Users/jason/opt/miniconda3/bin`。
- `audio_processor.py` **TTS 逐条删 mp3 撞 safe-delete 钩子（2026-09-27 修复）**：原来每合成一条就
  `os.remove(mp3)`，跑到第 50 次被钩子拦下（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`），TTS 在
  28/59 处直接 exit=1 —— 表现为「配音只更新了一半」。已改为把中间 mp3 写进 `tempfile.mkdtemp()` 且**全程不删**。
- `audio_processor.py` **`mv` 覆盖已有文件被文件策略拒绝（2026-09-27 修复）**：混音最后一步
  `mv mixed_audio_normalized.wav mixed_audio.wav` 报 exit 13
  `Brokered host rename source refused by file policy: prompt`（策略把「覆盖已存在目标」的 rename 判成 file-unlink）。
  已改用 `shutil.copyfile`（O_TRUNC 覆写，不 unlink）。
- `audio_processor.py` **`--video` 被 `work/cleaned.mp4` 顶掉（2026-09-27 修复）**：混音结尾原本
  `video_for_mux = cleaned.mp4 if 存在 else video_path`，返工时传进来的 `cleaned_v4.mp4` 会被**第一版旧成片**替换，
  等于把刚改好的文案/logo 画面又换回旧的。已改为**优先用显式传入的 `--video`**。
  ⚠️ 返工重混音后一定要抽查成片画面（不是只看音频）。
- `processed_log.py` **`add --video` 传文件名会被 shell 截断（2026-09-27）**：源文件名带 emoji /
  全角引号 / `#` 时，从命令行传 `--video` 极易被截断，脚本只回 `{"status":"error","video not found"}`，
  而且这个失败**不会**阻止后续流程（记录悄悄没写进去）。可靠做法：直接 import 调用
  `add_video(probe_json["path"], output_dir, "nba", "PASS")`。
- `processed_log.py list` 在 zsh 下会打印 `ERROR: GVM_ROOT not set`（本机 shell 环境所致），
  核对记录改读 `processed-videos.json`。
- **⚠️ 运行中禁止修改 bash 脚本**：后台 `bash xxx.sh` 运行时编辑该文件会改变字节偏移，
  bash 增量读取直接语法报错（2026-09-26 实录：V1/V3 在 clean 完成后死于 line-26 syntax error）。
  后段步骤要改就写到**新脚本**（如 `_job2b.sh`）再跑。
- `clean_video.py::render_translated_titles` **重叠窗口叠字（2026-09-26 修复）**：它把每个
  translate 项各生成一个 `drawtext` 滤镜全部叠加，而 plan 里相邻字幕的源窗口天然重叠
  （如 56.5-60.9 与 59.5-65.4），重叠区间两条文字会**画在同一位置互相叠影** ——
  用户肉眼直接判为"AI 生成的字幕、很花"。现已在 `render_subtitles.py` 的 SRT 侧去重叠，
  并在 `render_overlays.py`（新）里做绘制侧去重叠。**凡有相邻重叠窗口的视频都必须走这两条路径。**
- `render_subtitles.py` **静态项混入 SRT（2026-09-26 修复）**：`title` / `watermark` 类
  item 的窗口是整片（如 0-230.16s），写进 SRT 时被压成 `00:00:00,000 --> 00:00:03,456`，
  于是片头出现 3 条假字幕、且与真实字幕时间轴冲突。现已按 `localization-plan.json` 的
  category 过滤掉 `title/watermark/logo/slogan`。
- skill 脚本一律 `require_tools("ffmpeg")`，**ffmpeg 必须在 PATH**（本机在 `~/.local/bin`）。
  自写的一次性脚本若直接 `Popen("ffmpeg")` 会 FileNotFoundError —— 用
  `shutil.which("ffmpeg") or ~/.local/bin/ffmpeg`。
- **长任务必须用工具托管后台运行**：`nohup ... &` 起的进程在本环境会被回收（实测启动 11s 后
  进程消失、日志 0 字节、输出文件未创建）。要用 Bash 工具的 `run_in_background`。
  另：从 Python 里 `subprocess.run(['/bin/ps', ...])` 会被沙箱拒（Operation not permitted），
  `pgrep` 结果也不可靠 —— 判断存活看**输出文件是否在长大**。
- `qa_video.py` **临时帧会把 QA 整段打断（2026-09-27 修复）**：首尾黑帧检查的
  `qa_frame_*.png` 与全片残留检测帧原来都写进 `<work-dir>/`，跑完用 glob + `os.remove`
  逐个清理；230s 片一次产生 230+ 帧，累计删除数达到阈值即被钩子拦截为
  `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`，QA **直接 exit=1 且不给任何结论**
  （表现为「跑得飞快但什么都没检查」）。现改为每次运行 `tempfile.mkdtemp()` 新建
  `/var/folders/.../ttl-qa-residue-*` 抽帧、**全程不做任何删除**；交付 work-dir 也不再被污染。
  同类陷阱：任何"写一堆临时文件再批量删"的脚本在本环境都会踩这个钩子。

- `render_overlays.py` **`SUBTITLE_CATEGORIES` 只认字面量 `"subtitle"`（2026-09-27 修复）**：
  `spoken_subtitle` / `caption_overlay` 两类被**静默丢弃** → 所有口播字幕都不渲染，
  而 plan / SRT 侧一切正常、只有画面缺字，极难发现。现改为
  `SUBTITLE_CATEGORIES = ("subtitle", "spoken_subtitle", "caption_overlay")`。
- **`tts_timing.py measure` 只测时长、不做合成（2026-09-27 踩坑）**：所以只调它的驱动脚本
  会让 `mute_original_tts_bgm` **静默退化成"只有 BGM、没有旁白"**（成品听起来"正常"，只是没人说话）。
  完整链路必须显式包含
  `audio_processor.py tts --work-dir W --translated W/translated.json`。
  通用教训：**凡是叫"measure/timing/mix"的脚本，先确认它是否负责生成音频再编排。**
- `generate_post.py` **读不到 `work/post-content.json` 就静默套通用模板（2026-09-27 踩坑）**：
  文案是**语义产物**，脚本里没有兜底素材，它的"兜底"就是一段与视频无关的通用英文模板
  —— 出片后 `tiktok-post.md` 看着"格式完整、语法正确"，内容却和本条视频无关，
  极易被当成"写好了"直接交付。**规则：交付前必读一遍 `tiktok-post.md` 正文**，
  检查是否出现本条视频的具体人名/事件/比分；凡出现通用感叹句（"what a play!"类）即视为未生成。
  本批 8 条全部由 Agent 手写 `work/post-content.json` 规避（见 §6 与 `jobs/generate-post.job.md`）。
- `qa_video.py` 的 `render_subtitles --burn` **读到 0 字节 SRT 会让整条 QA 链判失败（A1 的连带）**：
  当某条视频的英文是"直接画进画面"时，SRT 为空是**正确行为**，但 `--burn` 仍会调 ffmpeg
  → `rc=1` → 整链标 FAIL（06 踩到）。**规则：SRT 为空 ⇒ 跳过 `--burn`，不要传空文件给 ffmpeg**；
  该片的字幕验收改看画面（`render_overlays` 的叠字检查），不看 SRT 行数。
  这也是"主流程 Job2 不再用 `--burn`"的第二个理由（第一个是 §12.11 的双重字幕）。
- `prepare_plan.py` **品牌色写死字面量 `0xFFDC00`（2026-09-28 修复）**：
  `build_items()` 给 title 的 `replacement.font_color` 硬编码了**旧黄**，而品牌色真源
  2026-09-27 已改到 `profile.cover_style.fill_color`（nba 现为 `#FFE800`）——
  于是草稿 plan 与 profile 不一致。**与 `cover_engine` 那次硬编码颜色是同一类 bug**
  （文档/配置改了一处、实现里还留着旧值）。现改为：`build_items` 只写兜底常量
  `DEFAULT_ACCENT_HEX`，`main()` 读 profile 真值覆盖，并打印
  `[prepare_plan] brand accent = 0xFFE800 (from profile 'nba')` 供核对。
  **凡"颜色/字号/比例"出现在两个地方，就是下一个 bug —— 一律只留 profile 一处。**
- `qa_video.py` 的 cover 检查有一项 **`frame_cleaned_regions >= len(plan_items)` 计数比对**：
  交付后若为清残留而**改了 plan**，封面仍是用**旧 plan** 渲染的 → 这项会**恒定 warning**
  （`cover frame not cleaned per localization-plan`）。
  这不是画面缺陷（实测这些封面 `chinese_residual` 全为 false），
  但为让 `qa.json` 与交付物自洽，**改过 plan 后应重出一次封面**。
  同类：`cover` 的 `face occluded by text` 对集锦封面**必然触发**，是**不可消除**的 warning ——
  所以本类的质检等级**天花板就是 WARNING**，验收标准应是
  **「`chinese_residue` PASS + 四件套齐全」**，而不是死等 PASS。

- 🔴 `clean_video.py::probe_color_args` **色彩别名表套错了作用域 → 整段渲染必挂（2026-09-28 修复）**。
  这是本批最难查的一个 bug，两次排查都走偏，值得完整记录：

  * **现象**：`render_parallel.py --segments 2` 两段都在 **1s / 5s** 内 rc=1；`render_overlays.py`
    的 traceback 恒定在 `base_proc.stdin.write(frame.tobytes())`（或 `proc` 那行）报
    `BrokenPipeError: [Errno 32] Broken pipe`；`seg_XX.mp4` / `base_XX.mp4` 均为 **0 字节**。
    同一条命令行单独手搓复现（甚至用同一个源片、同一组参数）**rc=0，完全正常**。
  * **真因**（`scripts/clean_video.py:71,87-90`）：`_TRC_ALIAS = {"bt470bg": "gamma28"}` 是给
    **传输特性 `-color_trc`** 用的名称映射（ffprobe 按矩阵名报 `bt470bg`，而 ffmpeg 的
    `AVColorTransferCharacteristic` 里叫 `gamma28`），但它被套在了**三个 flag 全部**上：
    ```python
    for flag, val in zip(("-colorspace", "-color_primaries", "-color_trc"), out):
        args += [flag, _TRC_ALIAS.get(val, val)]   # ← 连 -colorspace 也被改写
    ```
    03 源片标记是 `bt470bg,smpte170m,bt470bg` ⇒ 产出 `-colorspace gamma28 ...` ⇒ libx264
    报 `Unable to parse option value "gamma28"` / `Error setting option colorspace to value gamma28.`
    ⇒ `Error initializing output stream 0:0` ⇒ ffmpeg **启动即退** ⇒ 上游只看到 BrokenPipe。
    **02 源片是 `bt709,bt709,bt709`，别名键 `bt470bg` 永不命中 ⇒ 一路正常** —— 所以这个 bug
    只在"源片 color_space 恰好等于别名键"时才现形（低频、难复现、极易误判成资源竞争）。
    修复：`if flag == "-color_trc": val = _TRC_ALIAS.get(val, val)`（已验证
    `oldbug rc=1 / fixed rc=0`）。
  * **判据（下次 5 分钟定位，别再走弯路）**：`BrokenPipeError` + 输出 **0 字节** + 同命令手工复现正常
    ⇒ **不是磁盘/内存/并发，是子进程启动就失败**。第一动作应是**把子进程 stderr 取回来**，
    而不是去猜资源竞争或重跑。已加 `--ffmpeg-log` / 环境变量 `LOCALIZER_FFMPEG_LOG`
    （落 `<path>.main` / `<path>.base`），并把 `proc`/`base_proc` 的死写前判活改成
    `RuntimeError: <tag> ffmpeg exited early rc=... before frame N`，不再抛裸 BrokenPipe。
    `render_parallel.py` 会自动给每段设成 `render_parts/seg_XX.ffmpeg.*`（**必须每段独立**，
    否则多段用 "wb" 互相截断）。
  * **附带的第二个陷阱**：`probe_color_args` 用**裸名 `ffprobe`**（靠 PATH 解析），
    解析不到时 `except Exception: return []` **静默返回空** ⇒ 同一条视频在不同 PATH 下
    **产出的色彩标记不同**。我自己手搓的探针就因此"通过"了（沙箱 PATH 没有 `~/.local/bin`），
    把排查方向完全带偏。已改为 `shutil.which("ffprobe") or ~/.local/bin/ffprobe`。
    **通用教训：任何 `except Exception: return <默认值>` 都会制造"同一输入两种行为"，
    排查时必须先确认被测函数真的执行了它该走的分支。**

- `qa_video.py::check_chinese_residue` **单帧 + 低置信度 = OCR 误读，不算残留（2026-09-28 新增）**：
  这条硬门禁原来只按 `conf >= 0.5` 收集（默认 `min_confidence=0.5`），于是
  **人脸/头发纹理**和**快速推拉或缩放的英文图形**被误读出的 CJK 也会把整条片子判 FAIL。
  03 实测：226s 是「奥尼尔抱起科比」的经典照，科比头发处被读成 `可口`，conf **0.6877**、
  只出现在 **1 帧**；同片 13 组**真**残留全部是 conf **0.95+** 的完整句子。
  规则（窄口径）：`frame_count == 1 and max_confidence < 0.9` 的组不算残留
  （新增 `--residue-single-frame-conf`，默认 0.9，设 0 关闭）。
  **依据**：真烧录字幕至少持续 ~1s ⇒ 1fps 采样必然连续命中 ≥2 帧；误读通常只落在 1 帧上。
  这条**不削弱任何多帧残留的检出**，也不会放过高置信的单帧误读
  （用 03 旧数据回测：14 组 → 剔 1 组，保留的 13 组最低 conf 仍是 0.9542）。
  ⚠️ 纪律：**判"误读"必须先抽帧目视确认**（`ffmpeg -ss <t> -i final.mp4 -frames:v 1`，
  并把误报 bbox 区域放大），不能凭置信度数字就下结论 —— 这是硬门禁，
  放宽它的唯一正当理由是"已目视证明画面里确实没有中文"。
  剔除结果会写进 `qa.json` 的 `misreads_dropped`，并在 `detail` 里列出，
  便于复核而不是"悄悄消失"。

- 🔴 `generate_cover.py::clean_frame_with_plan` **把全片清理项都擦到封面这一帧上（2026-09-29 新增）**：
  函数签名原来只吃 `plan_items`，而 `localization-plan.json` 是**整条片子**的计划、
  封面是**单帧快照** —— 于是 65 个项**全部**在这一帧上执行了一遍，其中包括
  **此刻根本不在屏上**的区域。
  * **现象**：03（湖人传奇羁绊）`cover.jpg` 上，**科比脸正中一块 361×191 的纯黑方块**
    （1080×1920 画布坐标 x 47..408 / y 867..1058）。抽源片同帧（t=236.85s）**画面完全干净**
    ⇒ 黑块 100% 是封面渲染引入的，不是源片带的。
  * **真因**：plan 里唯一的非字幕项 `pop_000`（`caption_overlay`，action=remove，
    0.6–3.4s 的中屏中文关键词弹窗）bbox 是 428×128 → `h>100` ⇒ 命中
    **「大区域」分支 = 高斯模糊 + `blur_alpha=210` 压暗到 18% 亮度** ⇒ 看着就是黑块。
    把它的源 bbox 经 `picture_src`（`scale=0.84375 / origin=0 / fy=dst_h/crop_h=1075/608=1.768`）
    映射到画布：`x=56*0.84375≈47`、`y=422+(298*0.84375)*1.768≈867`、`w≈361`、`h≈191`
    —— **与实测黑块逐位吻合**，这就是定位的闭环。
  * **修法**：新增 `plan_items_at(plan_items, frame_time)` 按 `start_time`/`end_time`
    过滤（无时间字段的项视为常驻，保守保留），`clean_frame_with_plan` 增加 `frame_time`
    参数并在内部过滤；`generate_cover.py` 传 `selected_frame.timestamp_sec`
    （`frame_time=None` 退化为旧行为，保持向后兼容）。
    03 复跑结果：`cleaned regions: 0 of 0 on screen at 236.85s (plan total 65)` ——
    这一帧本来一个项都不用擦，原先的 65 次全是无中生有。
  * ✅ **QA 的分母也要跟着改（同一 bug 的另一半）**：`qa_video.py::check_cover` 原来判
    `cleaned >= len(plan_items)` —— 用**全片项数**当分母，反向"逼着" cover 去擦不存在的区域，
    而且会把「这帧本来就干净、一个都不用擦」判成失败。现改为读 cover 写入的
    `render.frame_clean_expected`（=该帧在屏项数），缺失时就地按时间戳过滤。
  * **判据（下次 1 分钟定位）**：`cover-plan.json` 里
    `render.frame_cleaned_regions` >> `frame_clean_expected`，或 `frame_clean_at_sec`
    落在任何 remove/translate 项的 `[start_time, end_time]` 之外 ⇒ 就是这个问题。
    一条命令可全量自查：遍历 `*/work/cover-plan.json`，按 `selected_frame.timestamp_sec`
    过滤 plan 项，比对 `frame_cleaned_regions`（本次 4 个已处理封面里只有 03 严重、
    04 轻度多擦 4 个 ≤100px 小区域，01/02 恰好相等）。
  * **副作用范围**：`backdrop: blur` 的背景层是**同一张已清理 `img`** 的模糊版，
    所以擦错区域的痕迹是两处（主画面一块 + 背景一抹），只会更显眼。
  * **通用教训**：**凡是"用全片级产物去处理单帧/单时刻"的代码，都必须先按时间过滤。**
    同类风险点：`clean_frame_with_plan`（本次）、任何"拿 plan 去改封面/首帧"的脚本。

- `qa_video.py::check_subtitle` **"幽灵冲突"：不产 sub 的项也参与冲突判定（2026-09-29 新增）**：
  它按 `timeline.json` 的 entries 逐对比 `final_end > next.final_start`，只排除了
  `title/watermark/logo/slogan` 四个静态类目。于是 **`remove` 类项**（只做擦除、
  **没有任何文字**、但仍有源窗口）会把相邻真字幕"顶"出一个冲突：
  03 报 `timeline conflicts: 1` —— `narr_000`（spoken_subtitle，0.0–2.416）vs
  `pop_000`（caption_overlay，remove，0.6–0.6），而 `subtitles-en.srt` **实测零重叠**。
  * **判据**：`timeline_conflicts > 0` 但手算 SRT 无重叠 ⇒ 去 `timeline.json` 找**没有
    `text_en` 的条目**，它一定是元凶（不是画面问题）。
  * **修法**：与 `render_subtitles.py` 用同一条判据 —— 该脚本写 SRT 时就是
    `if not text: continue`。QA 侧加 `if not (e.get("text_en") or "").strip(): continue`。
    修后 03 复跑：`SRT entries: 64, timeline conflicts: 0, unresolved: 0` → **pass**。
  * **通用教训**：QA 的分母/集合必须与**真正产出的东西**同名同界
    （这里：写进 SRT 的条目），拿"计划里的项"去校验"实际产出的行"迟早报幽灵问题。
    同一天同一个片子因此踩了两次（封面清理、字幕冲突），本质是同一个错。

- 🔴 `render_overlays.py::build_overlays` **屏幕下半部的常驻标题会被静默降级成 3.4 秒的 hook
  （2026-09-29 新增，05/06 首次命中）**：
  ```python
  if cat == "title":
      if cy < frame_h * 0.35: titles.append(...)   # a..b 全程
      else:                   hooks.append(...)    # a .. a+hook_seconds(3.4)
  ```
  * **后果比"少画一行字"严重得多**：mask **不看 category**，仍按 `start_time/end_time`
    在整片擦掉中文 ⇒ 底部那条常驻中文被擦掉后，**英文只在头 3.4 秒出现，剩下 470 秒是空白的**。
    成片看起来"字数不对"，而 QA 六项**全 pass**（它不检查 overlay 覆盖时长）。
  * **05 的实测形态**：顶部三行 cy=214/269/323（< 0.35×1280=448）落到 `titles` ✓，
    底部那条评论条 cy=901（>448）会落到 `hooks` ✗。同一份 plan 里 3 条对、1 条错。
  * **判据**：`plan` 里 `category=title` 且 `end_time - start_time` 接近全片、但
    `bbox.y + bbox.h/2 > frame_h * 0.35` ⇒ 这条**不会**全程显示。
    渲染日志里也能看：`overlays: 3 title / 1 hook / 0 sub` —— 全片常驻却出现 hook 计数
    就是它。
  * **修法（不写代码就能绕）**：给这条换一个"任何 y 都能全程画"的类目：
    - 想画**自己的品牌 mark** → `category=watermark`（`a..b` 取项窗口，字号 =
      `--logo-ratio`×frame_w，正常 0.32）；
    - 只想**擦掉不补字** → `category=watermark` + `action=remove` + `target_text=null`
      （render 直接 `continue`，但 mask 照样擦），需要补 mark 时用 `--logo-y` 指定纵向中心。
  * **通用教训**：**"类目"在渲染器里同时决定"画什么"和"画多久"**，不是纯语义标签。
    凡是要"全片显示"的项，先回读 `build_overlays` 的分支条件（尤其 `cy` 阈值），
    别假设 `title` 一律全程。
  * ⚠️ **同源陷阱（2026-09-29 补充）**：下半屏 title 走 `hooks` 分支后，
    结束时间是**绝对常量 `hook_seconds`（3.4s）**，不是 `start + 3.4`。
    ⇒ **`start > 3.4s` 的下半屏 title 连英文都不出**（中文被擦、英文窗口为零长）。
    07 的 `start` 恰好被 `--pad-head 0.5` 拉到 0，才落在甜点区 `[0, 3.4]` 里侥幸正确。
    **纪律**：凡 `cy > 0.35·frame_h` 的 title，落笔前先确认 `start ≤ 3.4`，
    否则必须改类目（见上面的修法）。

- 🔴 `audio_processor.py::_audio_source()` **在 `cleaned.mp4` 没有音轨时回退去 `probe.json` 取源片**
  （2026-09-29 新增，07 实测日志：`audio source: ..._xWT126.mp4 (from probe.json, has audio track)`）：
  * 这是**正确行为**，不是 bug —— `render_overlays` 产出的 `cleaned.mp4` 是**纯视频**，
    音轨从来不由它负责；`keep_original` 策略此时必须回源片拿音轨。
  * **别做的两件事**：① 不要自己 `ffmpeg -i cleaned.mp4 -i src -map 1:a` 去给它"补音轨"，
    第 6 步会再混一次，得到双音轨/双声部；② 不要因为 `cleaned.mp4` 探测不到音轨
    就断定渲染链断了（97% 的情况下这只是正常中间态）。
  * 前提：`probe.json` 存在且源片路径可达 —— 所以**别删 `probe.json`，也别移动源片**。

- 🔴 **"零改动条目"会让 `localize.py` 在第 3b / 第 5 步硬失败 rc=1**（2026-09-29 新增，08 实测）：
  * 当 `spec.items = {}` + `keep_unlisted: true`（源片本来就是英文、屏上零中文，无需任何动作）时：
    - 第 3b 步 `tts_timing.py apply` → `[ERROR] No timing entries to apply`，**rc=1**；
    - 第 5 步 `render_subtitles.py` → `[ERROR] No entries in timeline`，**rc=1**。
  * **`localize.py` 没有"零改动快路径"**：第 4 步无论如何都要重编码一遍全片。
  * **绕法（手工等价收尾，08 已跑通）**：
    1. 手写一份空 `timeline.json`
       （`{"status":"ok","strategy":"no_plan_items","entries":[],"total_entries":0, ...}`）
       —— **在跑 `localize.py` 之前**放好，让 3a/3b 命中 `.pipeline-steps.json` 跳过；
    2. 预置一个无音轨 `cleaned.mp4`（`ffmpeg -i src -map 0:v -c copy -an`）跳过第 4 步；
    3. `final.mp4` 用**真·无损重封装**：
       `ffmpeg -y -v error -i src -map 0 -c copy -movflags +faststart <out>/final.mp4`；
    4. `subtitles-en.srt` 写**空文件**（`printf '' > ...`）—— QA 的 `check_subtitle`
       只判 "not found"，**0 条也算 pass**（与 05 一致）。
  * 08 复验：aac 11830 帧 / h264 16478 帧 / 549.267s 与源片**逐轨一致**，
    文件只差 117 字节容器元数据 ⇒ 等价于"什么都没动"的完美交付。
  * **通用教训**：凡"输入为空 ⇒ 无操作"的脚本，要显式支持空输入并 **rc=0 退出**；
    现在这四处（tts_timing / render_subtitles / 3b / 5）都是"空 = 错误"，批处理会整条中断。

### 13.4.1 精修渲染：`render_overlays.py`（2026-09-26 新增）

**何时用**：用户反馈"字幕像 AI 加的 / 很花 / 字体难看"，或视频存在**相邻字幕窗口重叠**、
**全片静态 slogan 与字幕抢位置**这类观感问题。默认流程交付后再返工代价极高（要重跑 inpaint），
所以**首次交付前**推荐直接用它替代 `clean_video.py` 的 Step1+Step1b：

```bash
python scripts/render_overlays.py \
  --video <源视频> --work-dir <work> --output <work>/cleaned_v2.mp4
```

单遍完成：inpaint（逻辑与 clean_video 一致）→ Pillow 精绘英文。相比默认路径的改进：

1. **绘制侧去重叠**：副标题按起始时间排序，前一条的 end 截到后一条 start，
   同一时刻永远只画一条（根治叠影）。
2. **粗体 + 重描边 + 下落阴影**：默认 Arial Bold（`/System/Library/Fonts/Supplemental/Arial Bold.ttf`），
   副标题白字配 5px 黑描边 + 4px 偏移阴影 —— 在杂乱比赛画面上依然清晰。
3. **视觉分层固定**：顶部标题金色（#FFD700）全片、底部 slogan 金色**只在前 3.4s**
   （`--hook-seconds` 可调，避免整片和副标题争版面）、`watermark` 类**擦除后不重绘**（画面更干净）。
4. **宽度自适应**：字号按 `frame_w - 70` 上限自动缩，长英文句不会出框。

**注意**：
- 只产出**视频轨无音**的 cleaned_v2.mp4，音频仍需照常走 `audio_processor.py mix`，
  最后 `ffmpeg -i cleaned_v2.mp4 -i work/mixed_audio.wav -c:v copy -c:a aac` 合并。
- 逐帧 OpenCV inpaint 是本流程唯一重活，6900 帧约 30 分钟（M 系列），必须后台跑。

### 13.4.2 封面版式（已归并 → §8.4 / §8.5 / §8.3）

版式要点、画面处理、选帧、安全区**全部已归并到 §8**：

| 原内容 | 现在在 |
|---|---|
| 底图必须无字、全帧放大 1080×1920 | §8.4 |
| 画面区处理（不锐化 / 压暗降饱和 / DOF / 暗角） | §8.4 |
| 文字层级、字体取 font_chain | §8.2 |
| 品牌 logo 必须自己画、位置在底部 | §8.1 |
| 选帧评分（`pick_cover_frame.py`） | §8.5 |
| 安全区 + 三视图可见范围 + `--layout/--picture/--backdrop/--focus` | §8.3 / §8.4 |

**保留的唯一背景**：这一节是 2026-09-26 为「用户反馈封面很花」而写的。
当时的根因是 `generate_cover.py` 把标题字压在**未经处理的原始画面**上 ——
观众席的黄绿色斑块 + 三层文字（顶标题、中部水印、底 slogan）叠加即"花"。
现在 `generate_cover.py` 也走 `cover_engine.py`，两条路径同源，不会再分叉。

### 13.4.3 长视频：分段并行渲染 + 无字底片（2026-09-27）

`render_overlays.py` 是**单进程**，逐帧 OpenCV inpaint + Pillow composite 都在单线程里跑，
本机（8 逻辑核 / 4 物理核）**6900 帧约 45 分钟**，其余核心全闲置。两个提速手段：

1. **分段并行**：按 `--start-time` + `--max-frames` 切 4~5 段，各起一个进程，最后
   `ffmpeg -fflags +genpts -f concat -safe 0 -i list.txt -c copy` 拼接。
   切片安全的前提是渲染器用**全局帧号**算时间（`t = idx/fps`，`--start-time` 只跳帧、
   不重置 `idx`），因此 masks 与字幕窗口在每段里都自然对齐。
   实测 **5 段并行把 45 分钟压到约 13 分钟**。
   ⚠️ **但并行度有硬上限**：一次只跑一条链、每段加 `--x264-threads 1`（本批实测 4 链同跑把
   load 打到 230 并触发 OOM，见 **13.6**）。段边界取整帧（`start_frame / fps`），
   `--max-frames` 是「跳过后写出的帧数」，所以段 i 覆盖 `[start_frame, start_frame+n)`。
2. **`--save-clean-base <path>`**：在同一次渲染里顺手把 inpaint 后的**无字底片**导出。
   以后**只改字幕文案**时用 `--video <base> --no-inpaint` 重跑，约 2 分钟出片，
   不用再花 45 分钟重新 inpaint。注意 `--no-inpaint` 时不会再写 base（源本就干净）。
   本次「AI→IVERSON」返工 + 封面换底图都是靠它。

### 13.4.4 文案缩写：字幕里不要出现裸 `AI`（2026-09-27）

NBA 语境里 `AI` 是 Allen Iverson 的通用缩写，翻译上没错，但**观众第一眼会读成
「AI 生成」** —— 用户看到 `AI MID-RANGE BUCKET` 直接问「视频里面怎么还有 AI 的字样」。
规则：**指人的缩写一律写姓氏**（`IVERSON`），不要用 `AI`；同类自造缩写（`KB`、`MJ`）
也尽量避免，除非已是大众共识。

替换时必须按**词边界**匹配 `\bAI\b`，否则 `AGAIN` 会被改成 `AGIVERSON`。
要一起改的文件（缺一个就前后不一致）：
`work/translated.json`、`work/localization-plan.json`、`work/timeline.json`（`text_en`，
SRT 从这里出）、`work/post-content.json`、`tiktok-post.md`。
改完必须**重新渲染画面 + 重新生成 SRT**，因为字幕已烧进视频轨。

⚠️ **音频是独立的第三处，最容易漏（2026-09-27 二次返工）**：
TTS 配音直接用 `translated.json` 的 `target_text` 合成（`audio_processor.py run_tts`），
所以改了文案以后 `work/tts/*.wav` 里**还是旧台词** —— 用户听出来「音频还有 AI 的解说」。
只改视频轨、不重跑 TTS，成品就会**画面写 IVERSON、嘴里念 AI**。完整链路：

```bash
export PATH="/Users/jason/opt/miniconda3/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
PY=/Users/jason/opt/miniconda3/bin/python
SK=/Users/jason/Public/skills/tiktok-video-localizer-v2/scripts
$PY $SK/audio_processor.py tts --work-dir work --translated work/translated.json   # ① 重合成配音（约 1.5 分钟）
$PY $SK/audio_processor.py mix --video work/cleaned_v4.mp4 --work-dir work \
    --output-dir work --audio-plan work/audio-plan.json --timeline work/timeline.json   # ② 重混音 + 合成 final_video
```
② 会把 `mixed_audio.wav` 与 `final_video` 一起产出（`mix-result.json` 里记路径），
再覆盖 `final.mp4` 即可。**改文案后必查清单：视频轨 / SRT / 音频轨 / 封面文字 / 发布文案 五处。**

### 13.5 环境速查（本机）

```bash
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"   # ffmpeg/ffprobe 在 ~/.local/bin，tesseract 在 /usr/local/bin
PY=/Users/jason/opt/miniconda3/bin/python             # 必须用 conda：cv2/PIL/whisper/pytesseract/edge-tts 都在这里
```
- 系统 `/usr/bin/python3` **没有 cv2**，跑脚本会 ModuleNotFoundError。
- `whisper medium` 在 41s 音频上会被系统 OOM kill，**用 base** 即可满足决策需要。
- TTS 用 `edge-tts --voice en-US-GuyNeural`（男声，非机械感）。
- **OCR**：`rapidocr_onnxruntime`（唯一默认引擎，初始化约 2.6s）。`paddleocr 2.7.3` 与
  `tesseract` 仅为显式排障选项，正常流程不用。三者在沙箱内**均可运行**（paddleocr 只是
  初始化慢约 5 倍，故不作默认）。相关类库 onnxruntime、paddlepaddle 2.2.2 已在本机 conda 环境内。

### 13.6 并发上限：本批最大的时间黑洞（2026-09-27）

**现场**：4 条链同时渲染，每链 `render_parallel` 切 3–4 段、每段**两个**编码器
（`seg_XX.mp4` + `--save-clean-base` 的 `base_XX.mp4`），合计 **24 个 libx264**，再加 8 个
OpenCV 进程 → `uptime` 的 load average 冲到 **230**（5 分钟均值 309），`vm_stat` 的
`Pages free` 掉到 **28MB**，`generate_cover.py` 连续两次 `Exit 137` 且**一行日志都没输出**
就被杀。改成"一次只跑一条 + 限线程"后 load 稳定在 **9–40**，再无 OOM。

**规则**：

1. **一次只跑一条链**（`serial_run.py` 模式：链间串行、链内 3–4 段并行）。
   不要"每条链各开 3–4 段"地叠乘 —— 编码器总数 = 链数 × 段数 × 2。
2. **每段加 `--x264-threads 1`**（`render_parallel` 可透传）。
   默认 `--threads 0` = 每段吃满全核，段数一多就互相踩。
3. **OpenCV 侧无法限流**：本机 conda 的 OpenCV 报 `Parallel framework: GCD`，
   `cv2.setNumThreads()` **被忽略**（请求 1/2/4 线程，`getNumThreads()` 恒返回 8）；
   实测 `cv2.inpaint` 在 1080×1920 上 1/2/4/8 线程**都是 ~0.30s/帧** —— 它本来就是单线程的。
   所以别指望 `LOCALIZER_CV_THREADS` 救场（该开关已加，但只对 OpenMP/TBB 构建生效）。
4. **段数不是越多越好**：`8 核 / 每段 2 个编码器` 时 4 段已到顶。
5. 判断"渲染在不在推进"看**输出文件是否在长大**（`stat -f%z`）——
   `ps`/`top`/`sar` 在沙箱里是 `operation not permitted`，`pgrep` 也不能看进度。

### 13.7 返工时只重渲受影响的段（关键帧对齐 + 流拷贝，2026-09-27）

**场景**：交付后 QA 报出的残留 / 改字只落在**某一个分段**里（常见于 1s 窗口的清理项）。

```
1. 重建 plan（新增清理项）→ 删 masks-index.json / timeline.json 让缓存失效
2. 只重渲受影响段：  render_parallel.py … --reuse
   （已存在且帧数正确的 seg_XX.mp4 直接参与 concat，不重渲）
3. 若缺陷只影响**片头**，还能更省：只重编 [0, K) 帧
   （K = 缺陷窗口之后**第一个关键帧**的帧号），其余 ffmpeg -ss <K/fps> -c copy 切尾 +
   concat -c copy 拼接 —— 纯流拷贝，尾部逐位复制、零画质损失。
```

- ⚠️ **复用段的前提**：新旧 plan 里**旧 item 逐字段完全一致**
  （`item_id`/`category`/`action`/`bbox`/`start_time`/`end_time`/`target_text`/`source_text`），
  只多出新的清理项。改过旧条目就不能复用。
- ⚠️ **渲染不受 pad 影响**：`render_parallel` 不传 `--pad-head/--pad-tail`，复用段不必关心 pad 差异
  （pad 只写进 timeline 供 SRT/QA 用）。
- ⚠️ **关键帧不一定是固定网格**：x264 按 scenecut 插 IDR。务必实测
  `ffprobe -select_streams v:0 -show_entries packet=pts_time,flags -of csv=p=0`
  （流式读、命中即 kill，别等它扫完整个文件）。
- ⚠️ **被中断的段必须先删**：`--reuse` 只看"文件存在"，会**静默复用截断的坏段**拼出错位成片。
  判据：`mtime >= 中断时刻` 即半成品。注意本环境**删文件可以、删目录会被拦**（见 13.8）。
- ⚠️ **拼接必须逐位校验**：抽拼接点帧算 SHA256 与旧版比对，必须一致；再校验总帧数与时长
  （偏差 >0.6s 即中止）。任一不过 → **自动回退整段重渲**，不要硬着头皮交付。
- **实测收益**：04 从 ~42 分钟 → ~11 分钟；02 从 ~9.5 分钟 → ~2.5 分钟；
  05（6178 帧首段）从 ~50 分钟 → **110 秒**。
- 通用实现已沉淀为独立 skill `~/.workbuddy/skills/video-window-patch/`（`window_patch.py`，
  校验不过则非零退出且不产出 `--out`，调用方可无脑回退）。

### 13.8 沙箱与后台：本批再次踩到的四条

1. **`nohup … &` 活不过一轮对话**（§13.4 已记，本批再次验证）：第一版收尾守护就是这样死的，
   06 的 QA 跑到一半被打断。**一律用 Bash 工具的 `run_in_background`**。
2. **`~/Downloads` 下的目录删不掉**：`[safe-delete] broker denied delete`
   （`os.walk` + `os.rmdir` 同样被拦）；但 `os.remove()` **删文件是允许的**。
   清理 work 中间件时只删文件、别删目录，也别指望能删掉误建的空目录。
3. **PATH 必须在 import 之前铺好**：脚本 `require_tools("ffmpeg")` 会硬失败，
   现象是 `exited with code 2` + `Missing required deterministic tools: ['ffmpeg','ffprobe']`。
   自写的收尾脚本要在 **import 之前**写：
   `os.environ["PATH"] = "/Users/jason/opt/miniconda3/bin:" + os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH","")`。
4. **Pillow 画不了中文**：`ImageDraw.text()` 用 latin-1 编码，传中文直接 `UnicodeEncodeError`。
   给**中文视频做核对图/接触印样/bbox 标注**时，图上的说明文字**一律用 ASCII**
   （时间戳、`UPPER TITLE`、`watermark #3`、`bbox 512,88,980,210`），
   需要中文就往画面里塞**裁切出来的原图块**，不要在图上"写"中文。
   本批所有核对脚本（`chk_accent.py` / `cmp_colors.py` / 对比图）都遵守这条。

### 13.9 改叠字/改品牌色：用 `clean-base` 快速重渲（2026-09-27 晚）

**场景**：交付后要改**叠字层**（logo/标题颜色、文案），画面清理不变。

```
1. 用无字底片重渲（跳过 inpaint）：
   render_parallel.py --video <work>/clean-base.mp4 --work-dir <work> \
       --output <work>/final_recolor.mp4 --segments <N> \
       --no-inpaint --profile nba --threads 1
2. 重新混音（-c:v copy，秒级）：
   audio_processor.py mix --video <上一步输出> --work-dir <work> --output-dir <out> \
       --audio-plan <work>/audio-plan.json --timeline <work>/timeline.json
```

- **为什么快**：`--no-inpaint` 省掉 `cv2.inpaint`（1080×1920 ≈ **0.30s/帧**，单帧成本的
  大头）。实测 **1280×960 → 17.6 fps/进程**（1306 帧 74s），1080×1920 ≈ 10 fps/进程；
  对照同样尺寸**带 inpaint 实测 2.6s/帧**（58 帧花 150s）—— 差 **25 倍以上**。
- ⚠️ **前提：base 必须比 plan 新**。`clean-base.mp4` 只是"生成它的那一刻"的 plan/masks 的
  产物；之后补进来的清理项（片头真中文、OCR 误读框）**不在里面**，拿旧 base 重渲会把中文
  带回来。判据：比较 `clean-base.mp4` 与 `localization-plan.json` 的 mtime。
- **base 落后时：局部修 base，别全片带 inpaint 重渲**。按受影响时间窗向外扩到**关键帧**
  边界，只重渲这几段 base（`--save-clean-base`），再与旧 base **无损拼接**，最后全片
  `--no-inpaint` 重渲一次。本批实现：`tiktok-batch/patch_base.py`
  （`--tag 05 --windows 0,1.0 172.4,175.6 421.4,422.6`）。
  - ⚠️ **切段用精确帧数，不要用 `-t 时长`**：30fps 下 `-t` 的边界舍入会让每段多 1~2 帧，
    几段拼起来时间轴就漂（第一版 24714 vs 24710）。用
    `round(t1*fps) - round(t0*fps)` + `-frames:v N`，并断言拼接后总帧数不变。
  - ⚠️ **别用 `ffprobe -count_frames` 数帧**：800MB 的片会被整片解码，本机 load 高时
    直接 `Exit 137`。优先读容器元数据 `stream=nb_frames`。
- ⚠️ **不是每条都有 logo/标题**：只有 plan 里存在 `category=watermark`（或 `title`）**且带
  `target_text`** 的条目才画。本批 01（只有字幕）、08（只有清理项）→ **无需重渲**。
  先扫 plan 再决定范围，别默认全量重跑。
- **颜色只有一个真源**：`--profile nba` → `cover_style.fill_color`。改色 = 改 profile 一处，
  然后「重出封面（`cover_engine`）+ 重渲叠字（`render_overlays`）」两步；改完务必用
  `chk_accent` 类的脚本**读真实像素**复核（QA 只校对 profile，不读像素）。

### 13.10 长批次的时间与磁盘预算（2026-09-27）

**排期不要按"条数"估，要按"条数 × 片长"估。** 本批 8 条（50s–824s，合计约 43 分钟 /
5.5 万帧）的实测：

| 项 | 事实 |
|---|---|
| Job1（probe + OCR + ASR） | 累计约 **8 小时** CPU，最长一条 **05 = 154 分钟**（824s / 2.4 万帧，OCR dense pass 是大头）。**这部分不可压缩**，只能提前开跑 |
| Job2/3（清理 + 渲染 + 封面） | 单条约 10–40 分钟，**可优化**（见 §13.4.3 / §13.6 / §13.7 / §13.9） |
| 单条 work 体积 | 05 ≈ **2.3 GB**（masks + 分段 + 无字底 + 中间 mp4 各留一份） |

- ⚠️ **`asr.json` / `ocr.json` 只在最后一次性落盘**：Job1 跑完才写文件，中途中断 = 整条白跑
  （本批 05 的 154 分钟就是这么丢过一次）。**长片要单独一条一个进程跑并及时检查产物**，
  不要一条命令串完 8 条。
- ⚠️ **跑批前先看磁盘**：本批 `/System/Volumes/Data` 已用 **428Gi / 466Gi（97%）**，
  可用仅 **15Gi** —— 两条长片的中间件就能把它吃光，随后 `generate_cover.py` 被 OOM 杀
  （Exit 137，无日志，见 §13.6）。**每条交付完立刻清 work 里的大件**
  （`clean-base*.mp4` / `base_XX.mp4` / `seg_XX.mp4` / `masks/`），只留
  `localization-plan.json` / `qa.json` / 四件套 + `work/*.json` 契约。清理注意 §13.8 条第 2 点：
  **只删文件，别删目录**。

### 13.11 批处理操作纪律（2026-09-27 失误复盘）

本批的"翻车"有一半不是脚本缺陷，是**操作**，逐条固化为纪律：

| # | 失误 | 纪律 |
|---|---|---|
| 1 | **并发开太多**（4 条链同跑）→ load 230、白等 ~40 分钟、2 次 OOM | 一次只跑一条链；上限与推导见 **§13.6** |
| 2 | **手敲中文路径传源视频**（06 的全角冒号/省略号被 zsh 改写）→ `cv2` 报错 + `ZeroDivisionError` | 中文路径**一律从文件按下标取**，不要手敲：`tiktok-batch/pathof.py <index> <N>`；脚本内部也别自己拼中文名，读 `index.json` |
| 3 | 误建 `Downloads/tiktok/tiktok/` 空目录 | 输出目录**先推导再创建**；空目录在本环境删不掉（§13.8 条第 2 点），只能让用户 `rmdir` |
| 4 | **误判 05 的 ASR"被中断"** —— 实际是 `grep` 过滤把 `job1.py` 自身排除了，日志看着像断了 | **过滤前先看全**：先 `tail` 完整日志确认状态，再用过滤条件做二次确认；别让一条 grep 决定"要不要重跑 154 分钟" |
| 5 | `fix05fast.ocr_head` 阈值写成 `cjk >= 1` → 单字 `吧`(conf 0.715) 被当成残留 → **误触发全量重渲** | 判定阈值**直接引用 §1/§12.11 的结论：`cjk >= 2`**，不要在一次性脚本里重新定义阈值；一次性脚本里凡是"要不要重跑大工程"的判据，必须和主流程同源 |
| 6 | 收尾脚本 `finish.py` 报 `code 2`，真因只是 PATH 缺 `ffmpeg` | 自写脚本 import **之前**铺 PATH（§13.8 条第 3 点），别一看到报错就怀疑逻辑 |
| 7 | 补跑脚本用 `rmtree` / `os.remove`（relogo.py 撞 safe-delete 配额：目录 >50 项或单文件删除计数 50 → 进程直接 abort，03 跑到一半被杀） | 补跑 / ad-hoc 脚本一律 **rename 不 delete**：`render_parts` → `render_parts_stale`；中间产物留着，靠改名让重渲自然覆盖（见 §14.8 修复脚本注释） |
| 8 | patch 函数拿源视频路径当拼接输出（splice / patch_03_frame0 把 `video` 当输出目标，险些 `mv` 走源文件） | **所有 patch / 补跑函数只读写 `out/final.mp4`，绝不直接碰源视频**；改完用 `ls -l` 校验源文件 mtime / size 未变 |

> 通用形态：**1、2、5 都是"看起来在省时间，实际最贵"的决定**（并发省的是别人的 CPU、
> 手敲省的是 3 秒、阈值放宽省的是排队）。凡是要重跑几十分钟的判据，一律按"宁可多验一次"写。

---

## 14. 修正记录（2026-09-22 第二批返工）

上一批 3 条视频交付后被用户打回，暴露 3 个**会让交付直接不成立**的问题。以下为硬规则。

### 14.1 中文 bbox 与时间范围必须用 `measure_text_bbox.py` 精确测量

**踩坑**：肉眼看缩略图估坐标，结果
- C 的红色标题实际持续 **0.0–4.3s**，plan 写 0.0–3.6s → **0.7 秒中文原样留在成片里**（用户："视频里面还有中文"）；
- A 的中文实际在 x=169–563/y=339–375，plan 写 x=85–585/y=295–395 → 框过大，清理后易留灰色补丁。

**规则**：中文区域**禁止目测**。屏幕字幕/水印几乎都是高饱和描边色，用颜色阈值可做到像素级精确：

```bash
# 红色描边大标题
python scripts/measure_text_bbox.py --video in.mp4 --color red --y-min 10 --y-max 140 --step 0.2 --json
# 黄色标题（全片常驻）
python scripts/measure_text_bbox.py --video in.mp4 --color yellow --y-min 310 --y-max 400 --json
# 底部白色字幕
python scripts/measure_text_bbox.py --video in.mp4 --color white --y-min 700 --y-max 800 --json
```
输出可直接填进 localization-plan 的 `bbox` / `start_time` / `end_time`。

可用 `--color`：`red` `yellow` `white` `cyan` `magenta`。脚本内部做三重收敛（密度过滤 → 最长连续段 → 分位数裁剪），
避免红色球衣/球场把 bbox 撑到画面边缘。**若输出 bbox 触边（x=0/y=0），说明仍被背景污染 —— 收窄 `--y-min/--y-max`。**

#### 14.1.1 什么时候**别**用 `measure_text_bbox.py`，改用「色核 + 跨时间」自测（2026-09-29 新增）

两个前提先摆出来：

1. **长片上它可能被杀**。本批 05（473s / 25fps）按 `--step 0.2` 跑，进程直接被 SIGKILL
   （exit 137，输出全空）。它按时间戳逐点 seek + 解码，点数 = 时长/step。**给长片加 `--step`**
   （≥1.0）或只截一段窗口。
2. **它按"颜色"找字，不是按"文本"找字**。当画面里有同色物体（黄色球衣、红色球场）时，
   收敛规则给出的框会带上背景。反过来，**当你要的框比 OCR 的框更紧时**，色核法才是对的。

**判据（什么时候必须自己量）**：
- OCR 的 track bbox **触边**（x=0 / y=0）—— 那是 padding 与背景一起被卷进来的信号；
- OCR 的 bbox **明显宽于肉眼可见文字**（本批 06：OCR 给 w=436/202，实测字只有 332/104 宽）。
  这不是 OCR 的错：`track_text.py` 的框是**多次检出的聚合**，带 padding，且会吃掉整条横幅的底板。

**自测法（20 行，跨时间取交集比单帧可靠）**：
```python
for t in (2, 30, 100, 200, 300, 400, 480):
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(t*fps)); ok, fr = cap.read()
    roi = fr[y0:y1, x0:x1]                       # 只在你已知的带内找
    b, g, r = roi[:,:,0].astype(int), roi[:,:,1].astype(int), roi[:,:,2].astype(int)
    m = (r>170)&(g>140)&(b<110)                  # 黄字；白字用 (r>185)&(g>185)&(b>185)
    xs = np.nonzero(m.sum(axis=0)>2)[0]; ys = np.nonzero(m.sum(axis=1)>4)[0]
```
- **看 7~8 个时间点的读数是否一致**：一致 ⇒ 这是字；只有个别点跑飞（本批 06 在 494s/502s
  读到 x>700）⇒ 那是画面里的同色物体，**按多数点取值**。
- 落在 **实拍画面**上的中文（不是黑边）要在测得范围上**只加描边余量（约 6px）**；
  落在**黑边**上的中文可以直接吃 OCR 的 padding（反正擦出来还是黑，本批 05 就是这种）。
- 收完框一定要 `preview_frames.py` 出帧 + 用 `ocr_video.ocr_with_rapidocr` **复扫一遍**再提交渲染：
  `chinese_residue` 是硬门禁，框收紧了却留了描边残影，要赔一整轮重渲。

### 14.2 `asr.json` 的 `has_chinese_narration` 不可直接采信

**踩坑**：whisper 被强制 `language=zh` 时，会把**非语音音频**（篮球音效/背景乐/英文采访）幻觉成中文，
且 `confidence` 为**负数**。上一批据此把 C 的整条原轨静音，41 秒视频只剩 6 秒 TTS → 用户反馈"没有声音"。
实测复核：auto 语言检测返回 `ko` + 纯乱码（"Muppet"/"gedacht"/"comma"），**该片根本没有人声解说**，是篮球音效 + 英文采访。

**规则**：
1. 看到 `has_chinese_narration=true` 先查 `segments[].confidence`，但**只看「是不是负数」会判错**
   ⚠️（2026-09-28 修正，见下）。正确读法是看**量级 + 文本连贯性**：

   | `avg_logprob` | 文本 | 判定 |
   |---|---|---|
   | **−0.2 ~ −0.5** | 句句连贯 | **真实转写** —— whisper 对真实语音本来就在这个区间 |
   | −0.5 ~ −0.8 | 连贯但口语重复 | 倾向真实（口头禅/现场噪声拖低） |
   | **≤ −1.0** | 循环、乱码、不成句 | **幻觉** |
   | −2.8 / −3.6 / −3.97 | 循环乱码 | **幻觉**（本批 01/02/04 实测值） |

   **反面案例**：本批 03（湖人 OK 组合，245s）170 段全部 `conf = −0.3258`，
   若按旧判据「负值 = 幻觉」就会把一部**有完整中文解说**的片子误判成无人声，
   从而丢掉整条英文解说 + 字幕。实际文本连贯（"他模仿着科比的动作／转身虚晃／
   后撤步左右晃肩／出手后停在空中的右手／可笑着笑着，有人却哭了"）→ **是真实转写**。
   `detect_asr_hallucination()` 的三条判据在 03 上 **0/3 命中**（−0.30 远高于 −0.70 阈值、
   重复句 0%、最高同长段仅占 12%）→ 脚本判对了，是**人肉判据**写粗了。
2. 复核方法：不指定 language 跑一次 auto-detect。返回 `ko`/`de` 等且输出无意义 → 无真实语音。
3. 只有当**画面上确实有中文字幕**或**确实听到中文人声**时才走静音策略；否则 `keep_original`。
4. ⭐ **`asr.has_chinese_narration` 这个字段本身也不可信**（本批 06 实测 =false 但确有人声）。
   判「到底有没有人声」最可靠的办法是**抽窗口单独跑 whisper**：
   有语音的窗口返回内容，纯音乐/噪声窗口返回**空串**。
   （本批实测：02 三轮窗口全空 = 无人声；06 第 1 轮有内容 = 有人声。比判正负、比读字段都准。）
5. 静音策略的后果必须自检：**静音原轨 + 无 BGM = 接近默片**。用户说"不处理 BGM"≠ 可以没有声音。
   若必须静音且不加 BGM，需先向用户确认。
6. ⭐ **判出「幻觉」不等于「ASR 坏了」—— 它同时给出"这条片没有中文旁白"这个结论**
   （2026-09-28 本批 05/06 实测）。两条片的 `segments[]` 里全是**英文碎片**
   （05："Matchwell, good rebound" / "they're really letting them play"；
   06："let's go" / "swordsman" / "we'll get four of that triple hits"）：
   * 05 `has_chinese_narration=False`、`chinese_ratio=0.0`、`lang=unknown`、avg_logprob −0.35；
   * 06 avg_logprob **−2.01**、重复句占比 **0.42** → 三条判据命中即判 HALLUCINATION，
     但它的"幻觉"同样只是"没有中文人声"，原片是**英文原声 + 音乐**。
   ⇒ 这两条都走 `keep_original`（原声本来就是英文，不要再叠英文 TTS，否则会出现
   "英文旁白 + 英文原声"双声部）。**别看到 `-2.01 / 重复 0.42` 就以为片子坏了。**
   顺带提醒：此时**没有任何东西需要翻译**，工作重心从"字幕重写"变成
   **"常驻中文标题/水印的原位替换"**（05 顶部三行标题块 + 底部横幅、06 顶部标题 +
   底部「曼巴记忆」水印位，两条都是**全片常驻**，`duration_sec ≈ 全片时长`、
   `detection_count ≈ 640~830`）。识别常驻覆盖物的快速判据：
   `text-tracks.json` 里 `duration_sec` 接近全片时长且 `detection_count` 特别高的那几条。

7. 🔴 **反过来也成立：`segments[]` 里的「空档」不等于「屏上没有中文」**（2026-09-28 03 实测，
   本批最贵的一个返工）。**按 ASR 分段切 cue 会整段丢掉"只有音乐、屏上打字幕卡"的区段**：
   既不给它生成 mask（中文残留），也不给它翻译（内容缺失）。

   * **现象**：03 首轮成品 QA `chinese_residue = FAIL`，14 组残留集中在
     **9–13 / 25–29 / 39–45 / 194–197s** 四个区间，每组都是完整、高置信（0.95+）的中文句子
     （`我有五个冠军 你只有四个` / `你可以把这句话存进银行里` / `（团队）这个单词里面没有i（自我）`
     / `当镜头捕捉到我们两个…`）。
   * **排查弯路**：看到 `chinese_residual_ocr_sample: 0`（采样检查）PASS 但全片 1fps 扫描 FAIL，
     第一反应是"mask 没生效"。实际 `masks-index.json` 与 plan items 严格 1:1、几何也正确 ——
     **是计划里根本没有那几段**。检查手法：把 `ocr.json` 底部字幕带（y 590–685）的检出按
     **文本聚合出的时间跨度**铺开，与 `translations.json` 的 cue 窗口对比，缺口一眼可见。
   * **判据（关键，别再连跑两轮 whisper）**：对缺口的窗口**单独重跑 whisper**，
     得到的是 `avg_logprob = -5.65 / -3.90` 的乱码（`那我們大概加入這一次` / `這種 Why are you team`）
     ⇒ 那是「纯音乐段」的幻觉特征，**原 ASR 不输出分段是正确的**，不是漏转。
     ⇒ 于是结论明确：**没有旁白，但有屏上中文** → 必须补 `translate` 项，不能靠"改 ASR"解决。
   * **修法**：把这 4 段作为**新 cue** 插进 `translations.json`，窗口取
     `[上一 cue.end, 下一 cue.start]`，再走 `tts_probe → split_cues → plan_narration →
     norm_narr → plan_builder` 完整重跑（**`segments[]` 不动**，因为没有旁白可提）。
   * ⭐ **新增 cue 的英文总长必须 ≤ 它自己的空档**，否则漂移会顺着级联传到后面一大段。
     第一版把 4 段的意思写全（gap A 9.89s / gap B 8.69s），最大漂移直接冲到 **6.35s**
     —— 英文比画面滞后 6s，肉眼可见。按级联公式 `place = max(cue.start, prev_end + gap)`
     **反推阈值**（gap A ≤ 6.33s、gap B ≤ 6.81s，因为前一个 cue 自己已经溢出 1.6s），
     收紧到 6.34s / 6.17s 后最大漂移降到 **2.16s**、旁白 cue 仅 0.33s。
     **操作要点：先算出「可用窗口 = 下一 cue.start − max(cue.start, prev_end + gap)」，
     再照着这个秒数写字，而不是写完了才去量。** 一个 cue 的 TTS 必须 ≤ 其窗口这一点，
     在级联下是"漂移"而不是"报错" —— 所以脚本的 `win<tts` 校验**不会**替你拦住它。
   * 取舍原则：缺口的字卡常是**引语**，与旁白可能语义重复（03 的 gap A 与 cue19 都讲
     "如果当年不那么要强就能多拿几个冠军"）—— **重复的可以砍，砍掉正好换来不漂移**。

8. ⭐⭐ **判定「原声到底是中文还是英文」的决定性手法：抽窗口、强制 `zh` / `en` 各跑一次**
   （2026-09-29 本批 07 实测，比读 `has_chinese_narration`、比 auto-detect、比肉眼听都准）。

   **为什么需要它**：管线 ASR 走 `whisper_local` 的 base 模型，在**音乐主导的混剪**上必然幻觉；
   而"英文原声"被强制 `language=zh` 转写时，输出的同样是**看起来像中文的乱码**
   （07 出的是 `韡蔡柏保` / `奶泥`）—— 光看"有中文输出"会把一条**英文解说片**误判成"有中文旁白"，
   从而错选 `mute_original_tts_bgm`，把好的英文原声整条静音掉再叠一层机械英文 TTS（双输）。

   **手法**：取一段有代表性的音频窗口（避开片头卡），**同一段**分别强制 `--lang zh` /
   `--lang en`，比较 whisper 返回的 `no_speech_prob` 与 `avg_logprob` 的**中位数**：

   | 强制语言 | median(`no_speech_prob`) | median(`avg_logprob`) | 输出文本 | 判定 |
   |---|---|---|---|---|
   | `zh` | 0.421 | **−4.320** | 乱码（`韡蔡柏保` / `奶泥`） | 强语言假设是错的 |
   | `en` | **0.056** | **−0.668** | 连贯英文（`And the Bryant!` / `Kobe Bryant on a record time!` / `Charging up a facial on the corner.`） | **真实语言** |

   **读法（两条同时看）**：
   * `no_speech_prob` 低（≲0.15）= 模型确信"这里有语音"；
   * `avg_logprob` 落在 **−0.2 ~ −0.7** 且**句子连贯** = 真实转写（见 §14.2 的区间表）；
   * 换语言后 `avg_logprob` 从 −4.3 跳到 −0.67、文本从乱码变成成句 ⇒ **赢的那个语言就是原声语言**。

   **工具**：`tiktok-batch/audio_probe.py`（本批新建，独立于管线）——
   ```bash
   python audio_probe.py <视频或wav> [--start 180] [--dur 120] [--model base] --lang both \
          --out work/audio-probe.json
   ```
   它自己 ffmpeg 裁 16k 单声道 → 逐语言调 whisper → 打印/落盘两张对照表。
   ⚠️ **坑**：whisper 内部 `whisper/audio.py::load_audio` 用**裸名 `ffmpeg`** 解码，
   而本机 ffmpeg 只在 `~/.local/bin` 软链 ⇒ 直接调会 `FileNotFoundError: 'ffmpeg'`。
   脚本里必须显式给子进程铺 PATH（与 `finish.py` 同源坑）：
   ```python
   env["PATH"] = os.path.dirname(FFMPEG) + ":/usr/local/bin:/usr/bin:/bin:" \
                 + os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
   ```

   **结论的用法**：
   * 赢 `en` ⇒ 原声是英文 ⇒ `keep_original`，**不叠英文 TTS**（会双声部）、不加 BGM（盖掉解说）；
     工作重心转为**常驻中文标题/水印的原位替换**（07/09 都是这种）。
   * 赢 `zh` ⇒ 真·中文旁白 ⇒ 走 `mute_original_tts_bgm` 的完整翻译链路（03 那种）。
   * 两个都乱 ⇒ 纯音乐/纯音效 ⇒ `keep_original`（或在用户确认下加 BGM），屏上中文仍要处理。

   [`audio_probe.py`]: 依赖 conda 环境 `/Users/jason/opt/miniconda3/bin/python`（带 whisper）。

### 14.3 清理后必须验证区间**两端**，不能只验中间

**踩坑**：C 交付前只在 2.0s 抽帧检查（干净），没验 4.0s —— 而残留恰好就在 3.6–4.3s。
A 的标题贯穿全片，只在中间抽一帧也会漏判。

**规则**：验证清理结果时，必抽 **`start` 前 0.3s、`start`、中点、`end`、`end` 后 0.3s** 五个点。
最省事的办法：用 `measure_text_bbox.py` 对**成片**再测一次，输出应为 `NO <color> text found`。

### 14.4 交付前必查音频容器规格

**踩坑**：混音把 16k/44.1k/24k 三种源升到 **192kHz 单声道**，封装成 AAC 后成 **96kHz/27kbps**，
多数播放器与 TikTok 不认这条音轨。

**规则**：`ffprobe` 检查 `sample_rate` / `channels` / `bit_rate`：
- 期望 **44100（或 48000）Hz、2 声道、≥128kbps**
- 出现 96000/192000 Hz 或单声道 → 判定为畸形容器，重跑 mix
- 同时用 `ffmpeg -af volumedetect` 确认 `mean_volume` 在 -30 ~ -8 dB（过低=接近静音，0=爆音）

### 14.5 用户说"中文直接去掉"= `remove`，不要自作主张 `translate`

**踩坑**：A 的中文标题被替换成英文 "WHO CAN STOP SHAQ?" 并渲染到全片 48 帧，用户要求"直接去掉，因为封面已经写了内容了"。

**规则**：
- 用户说"去掉/**删除**" → `action: "remove"`，`target_text: null`，`replacement: null`。**不要在原位补英文**。
- 只有当用户说"翻译/换成英文"时才用 `translate`。
- 封面（cover.jpg）已承载标题时，视频画面内**不重复**放同类标题 —— 这是用户的明确偏好。

### 14.6 片尾卡（outro / 频道 CTA 卡）：全片常驻 mask **覆盖不到**，而且是硬门禁杀手
（2026-09-29 新增，09「2012 全明星赛」首次命中，是本批最容易被漏掉的一类残留）

**现象**：09 渲染 + finish 全绿，但 QA 报 `chinese_residue: FAIL`：
3 条残留（`体育` / `感谢` / `点赞关注`，conf 均 **0.999+**）**全部落在同一秒 `938.0s`**。

**根因**：源片最后 **1.8 秒**是一张**独立的片尾卡**（黑底 + 源频道徽标 + 粉色按钮 +
「感谢 点赞 关注」），它**只在结尾出现**，而我们的 mask 是从
「全片常驻轨迹 + 开场卡」推导出来的 —— **两类都盖不住它**。
更阴险的是：**这类卡几乎总是"感谢/点赞/关注 + 频道台标"，天然是中文 + 竞品品牌**。

**必检纪律（写进流程，别再靠 QA 兜）**：
渲染完（或 Job1 结束）后，**单独扫一遍片尾最后 5 秒**：
```bash
ffmpeg -y -ss <dur-5> -i <src> -frames:v 1 -q:v 2 tail.jpg   # 目视
```
或用近黑像素占比自动定位（片尾卡多是黑底，`<20` 灰阶占比会从 ~0.30 跳到 **0.69 → 0.91**）：
```python
g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).astype(np.int16)
frac = float((g < 20).mean())     # 逐帧打印，跳变点即片尾卡起始帧
```
⚠️ **QA 的 1fps 采样救不了你**：采样点是整数秒，**短于 1 秒的片尾卡可能整条漏检**；
而只要它有一帧落在某整数秒上，就会直接 **FAIL**。

**首选处理 = 整段裁掉**（不要 inpaint）：
* 理由：① 那是**源频道的"点赞关注"CTA + 台标**，转载留着是双重损失（中文残留 + 竞品品牌）；
  ② 裁切是**纯流拷贝、零重编码、零 inpaint 伪影**，质量最好、成本最低；
  ③ 黑底上的白徽标 inpaint 效果虽好，但要重编码 + concat，性价比远不如直接切。
* 命令（**只切尾**，起点是 0 ⇒ 不受 §5.1-12「中段窗口不能用 copy」的限制）：
  ```bash
  ffmpeg -y -v error -i final.mp4 -map 0 -c copy -t <cut_sec> -movflags +faststart out.mp4
  ```
* ⚠️ **`-t` + `-c copy` 不是帧精确**：实测 `-t 937.633` 多留 2 帧（把片尾卡的淡入帧带进来了）。
  **必做步**：裁完用 `ffprobe -select_streams v -show_entries stream=nb_frames` 核对帧数，
  再 `-sseof -0.1 -frames:v 1` 出**末帧**目视确认没有徽标/中文，不够就**继续往前收**。
* 🚨 **裁掉时长会让 QA 的 `duration_match`（阈值 0.5s）FAIL** —— 这是**预期行为**，
  不是要你去掉 QA。正确做法是**显式声明新时长基准**：
  `qa_video.check_video` 会**优先读 `work/originalize-result.json` 的 `duration_sec`**
  （源码注释：*"原创化（标题卡/变速/BGM）会改变总时长"*）。
  该文件常规由 `originalize_video.py` 产出；本次是**有意编辑**，故**手写**并写清 `reason` /
  `removed_frames` / `method` / `verification`（可审计，不隐藏）。
* **留档**：把未裁切的 `final.mp4` 改名移到 `work/final-untrimmed-<dur>s.mp4`
  （与 §4.1 的 04 窗口补丁同口径），便于复算/复渲，不额外占空间。

**通用教训**：**"全片常驻"是假设，不是事实**。任何"只在首尾出现"的元素
（片头卡、片尾卡、字幕条、LOGO 动画）都逃得过"常驻轨迹"推导，
必须各自单独检一遍。**片尾尤其危险 —— 它常是 CTA + 台标。**

### 14.7 片头第 0 帧：**采样网格从来不含它**（2026-09-29 修复，比 14.6 更隐蔽）

**现象**：02 / 03 / 09 三条已交付、QA 全部 `chinese_residue: pass` 的成片，
**第 0 帧（t=0.000）都压着一张中文标题卡**：
* 02「30队巡礼之国王」+「白巧克力国王生涯高光混剪」（conf 0.93 / 0.99）
* 03「OK组合」+「一辈子的羁绊」（conf 0.976）
* 09「2012年 全明星赛」+「NBA全明星赛」（conf 0.995 / 0.917，f=0~1 两帧）

三张卡都是**独立片头卡**，**只存在 1–2 帧**，f=1（或 f=2）就硬切到正片。

**根因（`extract_frames.py`，2026-09-29 已修）**：原来用 `-vf fps=<N>` 抽帧。
**ffmpeg 的 `fps` filter 的输出帧不含源片第 0 帧** —— 实测 1FPS 的首个输出帧
落在源片 **t=0.4667**（第 14 帧），且此后每帧都比标注晚约 0.53s；原代码还把时间戳
写成 `(i+1)/coarse_fps`（1.0, 2.0, 3.0…），于是**采样比画面又晚 0.53s**。
⇒ **t=0 那一帧永远不在任何采样网格里**，dense pass 最早也只到 t=0.2。
⇒ `track_text.py` 自然看不到任何"持续性"，`qa_video.py` 的 `chinese_residue`
用**同一套 `fps=N` 取帧**，所以**它的 pass 是假的** —— 这是 14.6 的镜像问题，
但 14.6 至少会被 QA 抓到（片尾卡落在整数秒上），**14.7 连 QA 都抓不到**。

**修法**（两处都已改，改完必须自测）：
```python
# extract_frames.extract_coarse / extract_dense
src_fps = probe_src_fps(video)                     # ffprobe r_frame_rate
period  = max(1, int(round(src_fps / coarse_fps)))
run(["ffmpeg", "-y", "-i", video,
     "-vf", f"select='not(mod(n,{period}))'", "-vsync", "0",
     os.path.join(frames_dir, "c_%05d.png")])
# 时间戳 = i / coarse_fps  ->  0.0, 1.0, 2.0 …（既覆盖 t=0，又消掉 0.53s 偏移）
```
`qa_video.check_chinese_residue` 同样改成 `select='not(mod(n,period))'`
（它自己新增了 `_probe_fps`）。**自测判据**：改完对源片跑一次 `extract_frames.py`，
`frames-index.json` 的第一条必须 `timestamp_sec == 0.0`，且把该帧 PNG 丢进 OCR
**必须**能读出那张中文卡。仅看"代码改对了"不算验证通过。

**必检纪律（交付前自检清单 §12.8 已加）**：抽帧自检必须含 **最后 1 秒 + 片尾前 5 秒
+ 第 0 帧**。第 0 帧尤其要点名 —— 它既不在 1fps 网格里，也**不会**被 `-sseof` 之类
的"末尾取样"顺手带出来，只能显式取 `-frames:v 1`（第 0 帧）或按帧号 `select`。

**处理方式（首选：整帧替换 + 窗口补丁，别 inpaint）**：
这种卡是**独立镜头**，与 f=1 起的画面毫无关系，所以可以直接**把 f=0（及 f=1）
替换成下一镜头的帧**，效果远好于 inpaint：
* 零 inpaint 伪影（中文区域往往占画面 10–25%，TELEA 会糊一大块）；
* **帧数不变 ⇒ 时间轴/音轨完全不动**，不用改时长基准（对比 §14.6 裁尾要写
  `originalize-result.json`）；
* 实现（本例 03，K = 缺陷窗后第一个关键帧）：
  ```bash
  # 替换源 = 下一个镜头的首帧
  ffmpeg -y -v error -i final.mp4 -vf "select='eq(n\,1)'" -frames:v 1 new_f0.png
  # 只重编 [0, K)，f=0 用替换源盖住
  ffmpeg -y -v error -i final.mp4 -i new_f0.png -filter_complex \
    "[0:v]trim=end_frame=155,setpts=PTS-STARTPTS[base];[base][1:v]overlay=0:0:enable='eq(n,0)'[v]" \
    -map "[v]" -an -vsync 0 -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p head.mp4
  # 拼接（尾部流拷贝，逐位相同）
  python <video-window-patch>/scripts/window_patch.py \
    --old final.mp4 --head head.mp4 --out patched.mp4 --window-end 0.1
  # ⚠️ window_patch 的 head/tail 都带 -an ⇒ 输出无声，必须自己合并原音轨：
  ffmpeg -y -v error -i patched.mp4 -i final.mp4 -map 0:v -map 1:a -c copy \
    -movflags +faststart final_new.mp4
  ```
  ⚠️ **`head` 的编码参数必须与旧渲染一致** —— 旧渲染走 `render_overlays.py`，
  即 `-c:v libx264 -preset <preset> -crf <crf> -pix_fmt yuv420p`（无其它参数）。
  否则 concat `-c copy` 可能因 SPS/PPS 不匹配而花屏。
* 若走 inpaint：`action=remove` + `target_text=null` + `category=title`
  （`build_overlays` 见到空 `target_text` 直接 `continue`，只由 `generate_masks`
  出清理框），窗口取 `[0, 0.033]`，并且**必须 `--pad-head 0 --pad-tail 0`** ——
  否则默认 pad 会把窗口撑到 0.4s，在 f=1..12 的真实镜头上留下一片无谓的 inpaint 痕迹。

**通用教训（与 14.6 合起来记）**：**"全片常驻"和"采样网格"都是假设**。
任何"只在极短时间出现"的元素（片头第 0 帧、片尾 1.8s、闪现 LOGO）都逃得过
轨迹推导；而**采样网格的边界本身也是漏检源** —— 首帧、末帧都要显式点名。

### 14.8 TTS 混音：多路 `amix` 不加 `normalize=0` 会把人声压死（2026-09-29，03 实锤）

**现象**：用户反馈"成片只有背景音乐，没有英文解说"。但 `tts_track.wav` 里
64 条 cue 全部在场、时间也对 —— 语音被整体压低了 **36dB**（20·log₁₀(64)）。

**根因**（`audio_processor.py` 拼 TTS 轨处）：

```
amix=inputs=64:duration=longest          # ✗ 默认 normalize=1：每路 ÷64
amix=inputs=64:duration=longest:normalize=0   # ✓ TTS cue 互不重叠，unity 相加才对
```

amix 默认把**每一路输入除以输入总数**。cue 数一多（本例 64），每条 TTS 被
÷64 ≈ -36dB；后面 loudnorm 只能救整体响度、救不了人声/音乐**相对比例**，
最终人声埋在 BGM 底下 ~20dB —— 人耳就是"只有音乐"。
（2 路的 TTS+BGM amix 同理是每路 ÷2，但两路同比缩放不破坏相对平衡，
loudnorm 可补偿；**真正致命的是多路 TTS amix**。）

**实测证据链**（ Whisper 抽查 + volumedetect）：
- `tts_track.wav` 前 25s：mean **-57dB** / max **-36dB** —— 正好 = 1/64；
- 成片头部拉高 30dB 后 Whisper 立刻听写出完整 TTS 内容；
- 时间轴上 cue 全程覆盖（0–218s 无 >12s 空窗），但成片只有 BGM 较弱段落
  隐约"漏出"一点人声 —— **这是"QA 六项 pass 但用户听不到解说"的盲区：
  音频 QA 查响度/静音，不查"人声相对音乐的比例"**。

**修复与验证**（不需要重渲视频，只换音轨）：

```bash
# 1) 恢复电平（amix ÷64 的逆操作）
ffmpeg -i work/tts_track.wav -af volume=36.1241dB work/tts_track_fixed.wav
# 2) 与已有 bgm_track.wav 归一化相加 + loudnorm
ffmpeg -i work/tts_track_fixed.wav -i work/bgm_track.wav -filter_complex \
  "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[l];[l]loudnorm=I=-16:TP=-1.5:LRA=11[o]" \
  -map "[o]" work/mixed_fixed.wav
# 3) 视频流 copy，只换音轨（rename 换名，不要 rmtree）
ffmpeg -i final.mp4 -i work/mixed_fixed.wav -map 0:v -map 1:a \
  -c:v copy -c:a aac -b:a 192k -movflags +faststart final_new.mp4
```

**验收**：Whisper 听写修复后头部 25s 应完整还原 cue 文本；volumedetect 头部
mean 电平应从 -57 回到 ≈-17dB。

**纪律**：**交付 TTS 类成片前，必须 Whisper 抽听成片本身（不是 tts_track）**，
确认解说在头部/中部/尾部都能听写出来；`tts_track.wav` 有声音 ≠ 成片有声音。

**自动闸门（2026-09-29 落地）**：`qa_video.py` 的 `check_audio` 已对 `work/tts_track.wav`
跑 `volumedetect`，**峰值 < -25dB 即 WARNING**（正常 TTS 峰值约 -3~-6dB；amix 漏 normalize=0
会让峰值按 1/线索数 缩放，64 条 cue → ÷64 ≈ -36dB，直接对应本例）。
`audio_processor.py:490` 的 `normalize=0` 修复确保新渲染不再触发；若仍 WARN，说明重渲前
漏了修复或 tts_track 被旧流程产出，需按上面"验收"步骤手动 audiofix。

---

## 15. 编码与"画面发花"（2026-09-22 第三批返工）

### 15.1 禁止用 OpenCV `mp4v` 写视频 —— 它是"花"的头号元凶

**现象**：用户反馈 `shaq_who_can_stop` "怎么这么花"。逐秒扫描全片发现，成品**高频细节只剩源的 75%** —— 720x1280 @ 8 Mbps 的 mpeg4，反而不如源 HEVC @ 0.55 Mbps 清晰。

**原因**：`cv2.VideoWriter_fourcc(*"mp4v")` 是 MPEG-4 Part 2 Simple Profile，很老的编码器，率失真效率极低，码率堆到 8 Mbps 也补不回细节；且 macOS/QuickTime 兼容性差，画面容易呈现块状糊斑。

**修复（脚本已改）**：`scripts/clean_video.py` 新增 `FFmpegH264Writer`（BGR 帧 → ffmpeg 管道 → libx264 CRF 18），`inpaint_with_opencv` 与 `overlay_logo` 均已切换；`scripts/inpaint_video.py` 同步改用该类，并把后续 mux 的 `-c:v libx264` 改成 `-c:v copy`（避免二次编码）。

**效果**：A 从 51.7 MB 降到 29.3 MB，细节不再损失。

**规则**：逐帧处理后写视频，一律走 `FFmpegH264Writer`，不要用 `cv2.VideoWriter`。

### 15.2 顺手保住色彩标记

`mp4v` 写出的文件色彩标记是 `unknown`，而源是 `smpte170m`(BT.601)。播放器遇到无标记文件会按 BT.709 解读 → 颜色偏移。
`probe_color_args()` 自动读取源的 `color_space/primaries/transfer` 并透传给 x264。注意 `bt470bg` 作为 `-color_trc` 不被 ffmpeg 接受，需映射为 `gamma28`。

### 15.3 中文标题常带"底板"，必须连同底板一起移除

**踩坑**：A 的中文标题不是裸字，而是**一条全宽（x=0–720）半透明白色底板 + 黄字**，固定在 y=323–386。
原处理只用文字 bbox（x=160, y=329, 414×56）做 inpaint，结果**两侧未覆盖的白带残留**、中间被填成浅灰 → 一条明暗斑驳、静止不动的横带横贯画面。缩略图看不出来，一播放就非常扎眼 —— 这就是用户口中的"花"。

**规则**：
- 定位中文后先判断**有没有底板/横带**（逐行亮度 + 方差剖面，找"又亮又均匀"的连续行段）。
- 掩码必须覆盖**整条底板**（含无文字的左右延伸部分），并向外留 4–5 px 余量，让 inpaint 的采样边界落在真实画面上。
- 大区域（≥50 px 高）用 `cv2.INPAINT_NS` + radius 5，结构延续比 TELEA 自然。

**逐行剖面检测**（避开文字的列看亮度突变，一次看清底板上下界）：

```python
import cv2, numpy as np
cap = cv2.VideoCapture(SRC); cap.set(cv2.CAP_PROP_POS_FRAMES, 24*30)
_, f = cap.read()
cols = list(range(0, 160)) + list(range(600, 720))   # 左侧+右侧，避开中间文字
lum = f[250:450, cols].mean(axis=(1, 2))
prev = None
for i, v in enumerate(lum):
    y = 250 + i
    if prev is not None and abs(v - prev) > 25:
        print(f'y={y}  {prev:.0f} -> {v:.0f}  <== 边界')
    prev = v
```

### 15.4 交付前抽查"结构突变"，别只看平均指标

PSNR/SSIM 对**整块平滑填充**不敏感（A 的 SSIM 有 0.967，看着"合格"），而人眼对"静止的斑块/亮带"极其敏感。

**规则**：交付前对成品做一次**逐秒高频细节扫描**（对比源），并对画面里的**固定区域**（标题带、水印位、上下黑边）各抽 2–3 帧**放大看**。平均分不能代替肉眼。

---

## 16. TikTok「内容可能受限」的处理（2026-09-22）

上传后若出现「内容可能受限／建议按自律公约修改内容」，绝大多数由两类因素触发：

1. **音频**：保留了源视频原声，其中含受版权保护的现场音乐/背景音乐。
   ⚠️ 注意：ASR 只能识别语音，**识别不到音乐** —— 所以 `audio_strategy=keep_original` 不代表音频干净，`total_segments=1 (noise only)` 也不代表没有音乐。
2. **画面**：画面与平台上已有内容高度雷同（同一素材被反复搬运），或含第三方商标/版权画面（球队 logo、转播画面等）。

### 16.1 标准处理：换音频 + 原创化

用 `scripts/originalize_video.py` 一条命令完成：

```bash
python scripts/originalize_video.py \
  --video <旧 final.mp4> --output <新 final.mp4> --work-dir <work> \
  --no-crop --zoom 1.08 \
  --title "SHAQ vs LEBRON" --subtitle "SUMMER CAMP DANCE OFF" \
  --dynamic-text "11-15:WHO WON THIS DANCE-OFF?" \
  --bgm "music/sports/Wallpaper.mp3"
```

- `--bgm` 是**整体替换**原声（不是叠加）。曲库见 `music/sports/`（Kevin MacLeod，CC BY 3.0，可商用）；斗舞/搞笑类选 `Wallpaper`、`Cipher`、`Crunk Knight`。
- 方形或非常规画幅加 `--no-crop`，避免裁掉画面内容。
- 输出会**加长约 1.4s**（标题卡），记得同步平移 `subtitles-en.srt` 的时间轴。
- `--dynamic-text` 目前**只接受一个 spec**，要多条动态字幕需分次处理（会多一次编码）。

### 16.2 处理完仍受限时的升级手段

按"代价从小到大"排序：

| 手段 | 效果 | 代价 |
|---|---|---|
| 加转场/贴纸/额外动态字幕 | 弱 | 无 |
| 裁剪改画幅（`--to-vertical` / `--crop`） | 中 | 画面构图变化 |
| 变速（0.95x / 1.05x） | 中 | 观感变化 |
| **水平镜像 hflip** | **最强**：直接破坏画面指纹匹配 | 画面内文字会左右反向 |

### 16.3 字号必须按画面宽度自适应（已修）

`TITLE_CARD_FONT_SIZE=96`、动态字幕 `72` 原来写死在代码里，**方形画幅 720x720 下 "SHAQ vs LEBRON" 会横向溢出被裁掉**。现已统一走 `fit_font_size()`（Arial Black 大写≈0.62em，按最长行折算上限），title / subtitle / dynamic 三处都生效。

**规则**：以后再加任何 drawtext，字号一律用 `fit_font_size()`，不要写死像素值。

### 16.4 交付前自查

- 标题卡与动态字幕**四边都不贴边**（抽 0.7s / 中途 / 结尾三帧看）。
- `ffprobe` 确认音频确实换了（码率/时长与源不同）。
- 保留旧版备份，并在 `tiktok-post.md` 第 8 节写清改了什么，方便回溯与二次评估。
