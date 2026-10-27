# Architecture - TikTok Video Localizer

## 五层架构

```
┌────────────────────────────────────────────────────┐
│  L1 SKILL.md（入口契约层）                           │
│  触发条件 / 输入输出 / 流程指令 / 异常与QA决策规则      │
├────────────────────────────────────────────────────┤
│  L2 Jobs（任务编排层）                                │
│  ① analyze-video  ② localize-video（主流程）         │
│  ③ generate-cover  ④ generate-post  ⑤ review-report │
├────────────────────────────────────────────────────┤
│  L3 Scripts（确定性执行层，Python + FFmpeg/OpenCV）   │
│  probe / frames / ocr / masks / clean / subtitles   │
│  audio / cover / post / qa / tts_timing / track    │
├────────────────────────────────────────────────────┤
│  L4 Prompts + Profiles（智能与风格层）               │
│  视频理解 / 分类 / 翻译 / Caption / BGM / Cover文案   │
│  nba.yaml / sports.yaml / generic.yaml              │
├────────────────────────────────────────────────────┤
│  L5 Configs + Templates（可替换配置与输出格式层）      │
│  providers.yaml / default.yaml / 三个输出模板         │
└────────────────────────────────────────────────────┘
```

## 核心分工原则

| 角色 | 负责 | 不负责 |
|------|------|--------|
| **Agent** | 思考与决策 | 任何确定性计算 |
| **Skill/Job** | 流程与编排契约 | 实现代码、模型绑定 |
| **Script** | 确定性执行 | 语义判断 |
| **Provider** | AI 能力实现与 fallback | — |
| **Profile** | 内容风格 | 处理逻辑 |
| **Template** | 输出格式 | — |

## 数据契约（核心 JSON）

所有文件落盘于 `work/`。

### probe.json
```json
{
  "status": "ok",
  "path": "input/source.mp4",
  "duration_sec": 92.4,
  "width": 1080, "height": 1920,
  "fps": 30.0,
  "has_audio": true,
  "audio": {"codec": "aac", "channels": 2, "sample_rate": 44100}
}
```

### ocr.json（两遍采样合并后）
```json
{
  "ocr_engine": "rapidocr_local",
  "degraded": false,
  "detections": [
    {
      "frame_id": "f_00032",
      "timestamp_sec": 3.2,
      "text": "科比完成了绝杀",
      "language": "zh",
      "bbox": {"x": 100, "y": 1600, "w": 880, "h": 90},
      "confidence": 0.94
    }
  ]
}
```

### text-tracks.json（跨帧跟踪后的证据包）
```json
{
  "tracks": [
    {
      "track_id": "t1",
      "text": "科比完成了绝杀",
      "language": "zh",
      "bbox": {"x": 100, "y": 1600, "w": 880, "h": 90},
      "start_time": 3.2, "end_time": 6.8,
      "position": "bottom_center",
      "persistence": "short",
      "trajectory": "static",
      "asr_text_overlap": 0.78,
      "asr_text_overlap_note": "lexical overlap only, NOT semantic",
      "semantic_match": null,
      "confidence": 0.94
    }
  ]
}
```

**重要**：`asr_text_overlap` 只是词汇重叠（中文 char-bigram Jaccard / 英文 word Jaccard），不是语义重合。真语义判断（`semantic_match`）由 Agent 完成，脚本产出时恒为 null。

### asr.json
```json
{
  "provider_used": "whisper_local",
  "degraded": false,
  "language_detected": "zh",
  "has_chinese_narration": true,
  "segments": [
    {"start": 3.2, "end": 6.8, "text": "科比完成了绝杀", "confidence": 0.91}
  ]
}
```

### localization-plan.json（★ 枢纽契约：Agent 写、Scripts 读）
```json
{
  "profile": "nba",
  "review_mode": false,
  "audio_strategy": "mute_original_tts_bgm",
  "bgm": {
    "asset_source": "user_input",
    "asset_path": "input/bgm.mp3",
    "style_hint": "epic"
  },
  "items": [
    {
      "item_id": "t1",
      "category": "spoken_subtitle",
      "action": "translate",
      "confidence": 0.96,
      "bbox": {"x": 100, "y": 1600, "w": 880, "h": 90},
      "start_time": 3.2, "end_time": 6.8,
      "source_text": "科比完成了绝杀",
      "target_text": "Kobe hit the game-winning shot.",
      "source_text_removal": true,
      "removal_method": ["inpaint", "clone_patch"],
      "replacement": null,
      "reason": "底部位置+短持续+与ASR词汇重合高"
    },
    {
      "item_id": "t2",
      "category": "logo",
      "action": "replace",
      "confidence": 0.93,
      "bbox": {"x": 850, "y": 50, "w": 180, "h": 60},
      "start_time": 0.0, "end_time": 92.4,
      "source_text_removal": true,
      "removal_method": ["inpaint"],
      "replacement": {
        "asset": "input/brand-logo.png",
        "fit": "contain",
        "scale": "auto",
        "position": "bbox_center",
        "padding": 0,
        "opacity": 1.0
      },
      "reason": "右上角+全片固定存在→品牌Logo"
    },
    {
      "item_id": "t3",
      "category": "ad",
      "action": "review",
      "confidence": 0.55,
      "bbox": {"x": 200, "y": 900, "w": 300, "h": 120},
      "start_time": 41.0, "end_time": 44.5,
      "source_text": "某品牌广告牌",
      "reason": "背景广告牌，删除风险高，进入review"
    }
  ]
}
```

**5-Action 模型**：`keep` / `translate` / `remove` / `replace` / `review`

**规则**：
- `action=review` 的项，clean_video.py 必须跳过且不视为失败
- `action=replace` 必须有非空 replacement.asset，否则脚本以退出码 1 拒绝
- `action=translate` 且画面内中文字幕：必须 `source_text_removal: true`（移除原文+背景修复+重渲染英文），禁止"中文还在、英文盖在上面"
- Logo replace 默认 `fit: contain`（保持宽高比，禁止拉伸变形）

### timeline.json（TTS 对齐后的最终时间轴）
```json
{
  "strategy": "original_timeline_master",
  "playback_rate_definition": "1.0=normal, >1.0=faster (1.10=+10%), <1.0=slower; allowed [0.90,1.15]",
  "entries": [
    {
      "index": 0,
      "source_window": {"start": 3.2, "end": 6.8},
      "window_sec": 3.6,
      "text_zh": "科比完成了绝杀",
      "text_en": "Kobe hit the winner.",
      "tts_raw_duration_sec": 4.3,
      "adjustment": {
        "method": "shorten_translation",
        "decided_by": "agent",
        "playback_rate": 1.0,
        "final_start": 3.2
      },
      "final_duration_sec": 2.7,
      "final_start": 3.2, "final_end": 5.9,
      "fits_window": true
    }
  ],
  "unresolved": []
}
```

**时间轴原则**：原视频时间轴是主约束，TTS 时长是调整依据。不是 TTS 时间轴完全覆盖原视频时间轴。

**playback_rate 数学定义**（Agent/Script/TTS Provider 统一语义）：
- `1.0` = 正常速度
- `>1.0` = 更快（`1.10` = 加速 10%，`1.15` = 加速 15%）
- `<1.0` = 更慢（`0.90` = 降速 10%）
- 允许区间：`[0.90, 1.15]`
- ffmpeg atempo 直接取该值

**tts_timing.py 职责分离**：
- `--measure`：测 TTS 时长、比对原窗口、报告 overflow/underflow，**不做任何决策**
- `--apply`：执行 Agent 已写入 timing-decisions.json 的方案并复检
- 缩短译文/拆句/调速由 **Agent** 决定（prompts/tts_adjust.md）

### audio-plan.json
```json
{
  "strategy": "mute_original_tts_bgm",
  "original_audio": {"action": "mute"},
  "tts": {
    "provider_used": "cloud_tts",
    "degraded": false,
    "voice": "en-US-male-energetic",
    "entries_from": "timeline.json"
  },
  "bgm": {
    "asset_path": "input/bgm.mp3",
    "asset_source": "user_input",
    "volume_db": -18,
    "loop": true,
    "fade_out_sec": 2
  },
  "loudness_normalized": true
}
```

**V1 音频策略（二选一，无声源分离）**：
- `mute_original_tts_bgm`：检测到中文旁白 → 静音原轨 + 英文 TTS + Instrumental BGM
- `keep_original`：无中文旁白 → 完整保留原声（现场声/欢呼/环境音）
- ASR 完全不可用 → 保守走 `keep_original` + WARNING

### qa.json
```json
{
  "overall": "WARNING",
  "checks": {
    "video": {"status": "pass", "detail": "无黑帧/花屏，时长一致"},
    "subtitle": {"status": "pass", "srt_entries": 24, "timeline_conflicts": 0},
    "cover": {"status": "pass", "face_occluded": false, "subject_occluded": false},
    "audio": {"status": "pass", "loudness_ok": true, "chinese_residual": false},
    "localization": {
      "status": "warning",
      "chinese_residual_ocr_sample": 0,
      "replaced_logo_visible": true,
      "watermark_region_recheck": "clean",
      "review_items_pending": 1,
      "timeline_unresolved": 0
    },
    "opening_frame": {
      "status": "pass",
      "detail": "第 0 帧是正片画面，无片头卡",
      "opening_static": false,
      "opening_static_delta": 38.78,
      "frame0_vs_cover_mae": 63.06
    }
  },
  "degradations": [
    {"capability": "asr", "used": "whisper_local", "reason": "cloud unavailable"}
  ],
  "review_items": [
    {"item_id": "t3", "summary": "背景广告牌(00:41)，置信度0.55，已保留原样"}
  ],
  "confidence_summary": {"auto": 11, "warning_band": 2, "review": 1}
}
```

## Provider 抽象与 Fallback

### AI 能力（走 fallback 链）
| 能力 | Fallback 链 | 完全不可用时 |
|------|------------|-------------|
| Vision | Agent Native → Remote → 仅 OCR+ASR 降级理解 | 分类照常，caption/cover 质量下降 → WARNING |
| ASR | Agent Native → Cloud → whisper_local | 保守 keep_original + WARNING |
| TTS | Agent Native → Cloud → local_tts_cli → skip | 静音原轨+无配音+字幕+BGM → WARNING |
| OCR | rapidocr_local（唯一默认，**不降级**） | 文字处理跳过，仅 ASR 字幕 → WARNING；不得断言「成品无中文」 |
| Inpainting | Remote → OpenCV → clone_patch → crop | 清理置信度低标 LOW，残留→WARNING |
| WebSearch | Agent Search → skip | 仅按 profile 选本地 BGM，不虚构 Trending |

### 确定性工具（不是 Provider，不参与 fallback）
FFmpeg / OpenCV / Pillow / NumPy / PyYAML — 永远本地执行，缺失=环境问题（退出码 2）。

### 通用兜底原则
1. 任何降级必须写入对应 JSON 的 `provider_used` + `degraded` 字段，`qa.json` 汇总
2. 不确定时宁可保留 + Warning，不可误删（confidence < 0.60 一律 review）
3. 所有 fallback 终点都是"产物仍生成 + 如实披露"，而非中断或假装成功
4. 热门不等于可商用：BGM 推荐必须标注 Licensing，V1 不下载商业音乐

## Confidence 三档

| 区间 | 行为 |
|------|------|
| `>= 0.85` | 自动执行 |
| `0.60 ~ 0.85` | 执行 + QA WARNING |
| `< 0.60` | 不执行删除，`action: review`，进入 review 报告；宁可保留+Warning，不可误删 |

## V1 Scope

### Must Have
- 完整分析链路：probe / 两遍自适应采样 / 本地OCR / ASR / text tracking / 证据包
- 5-action 分类模型，logo 独立于 watermark
- Logo replace（用户提供 brand-logo.png，contain 保持宽高比）
- 音频二策略：有中文旁白→静音+TTS+BGM；无→保留原声
- 时间轴闭环：原时间轴主约束 + TTS 测长 + Agent 调整
- Confidence 三档 + --review 模式
- 英文字幕（SRT + 烧录）、Profile 驱动 Cover
- BGM：用户提供 Asset 或本地曲库混音；推荐标注 Licensing
- tiktok-post.md 九段 Markdown、qa.json 五类检查、全链路 fallback

### Nice to Have
- 可选 AI inpainting Provider（接口预留，默认 OpenCV）
- tracking 的 moving trajectory 完整轨迹点
- Cover 多候选（2-3 张供用户挑）

### Future / V2+（明确不做）
- 声源分离（Demucs 等）、"压低原声+TTS"混音路径
- STTN / ProPainter / 大型本地 Vision/TTS 模型
- 自动下载热门商业音乐、音乐版权保证
- 自动发布、账号矩阵、多语言目标（V1 仅 zh→en）
- 全局高帧率 OCR
