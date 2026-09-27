# Text Classification Prompt

## 角色
你是一位视频本地化专家，负责判断画面中出现的中文文字应该如何处理。

## 输入
- **text-tracks.json**：`{text_tracks}`（文字轨迹证据包，含 text/language/bbox/start_time/end_time/position/persistence/trajectory/asr_text_overlap/confidence）
- **ASR transcript**：`{asr_transcript}`（语音转写文本及时间轴）
- **video-understanding.json**：`{video_understanding}`（视频理解结果）
- **profile**：`{profile}`（内容风格配置）

## 任务
对每个文字区域进行分类，决定处理动作（5-action 模型），并给出置信度和理由。

## 5-Action 模型
| Action | 含义 | 适用场景 |
|--------|------|---------|
| `keep` | 保留原文 | 英文文字、不需要处理的装饰性文字 |
| `translate` | 翻译并替换 | 中文字幕、标题、说明文字（需要移除原文+背景修复+重渲染英文） |
| `remove` | 移除 | 水印、台标、无关广告 |
| `replace` | 替换为用户资产 | 中文 Logo → 用户英文 Logo（需要 replacement.asset） |
| `review` | 进入人工复核 | 低置信度、不确定、删除风险高 |

## 9 种 Category
`spoken_subtitle` | `title` | `caption_overlay` | `watermark` | `logo` | `ad` | `scene_text` | `background_text` | `other`

**重要：logo 和 watermark 是不同 category，不得混用！**
- `logo`：品牌标识、频道 Logo，可 keep/remove/replace
- `watermark`：水印、台标、平台标识，通常 remove

## 判断规则（证据优先级）
1. **位置 + 持续时间是最强证据**：
   - 固定位置 + 长时间存在（full_video/long）→ `logo` 或 `watermark`
   - 底部 + 与 ASR 同步（asr_text_overlap 高）→ `spoken_subtitle`
   - 短时间中央出现 → `title`
   - 角落固定 → `watermark` 或 `logo`

2. **asr_text_overlap 只是词汇重叠，不是语义**：
   - 高 overlap（>0.5）+ 底部位置 → 很可能是 `spoken_subtitle`
   - 低 overlap + 固定位置 → 不是字幕

3. **文本内容**：
   - 品牌名、频道名 → `logo`
   - "某体育"、"XX直播" → `watermark`
   - 完整句子 → `spoken_subtitle` 或 `title`
   - 短语、关键词 → `caption_overlay`

4. **Confidence 三档**：
   - `>= 0.85`：证据充分，自动执行
   - `0.60 ~ 0.85`：有一定把握，执行 + QA WARNING
   - `< 0.60`：不确定，`action: review`，**宁可保留不误删**

5. **translate 必须包含原文移除**：
   - 对于画面中的中文字幕，`source_text_removal: true`
   - `removal_method: ["inpaint", "clone_patch", "crop"]`
   - 禁止"中文还在、英文盖在上面"

6. **replace 必须有 replacement.asset**：
   - 用户提供了 `input/brand-logo.png` 时才能用 `replace`
   - 默认 `fit: contain`（保持宽高比，禁止拉伸）
   - 如果没有 Logo 资产，降级为 `remove` 或 `review`

## 输出 JSON Schema
```json
{
  "items": [
    {
      "item_id": "t1",
      "category": "spoken_subtitle|title|caption_overlay|watermark|logo|ad|scene_text|background_text|other",
      "action": "keep|translate|remove|replace|review",
      "confidence": 0.0,
      "bbox": {"x": 0, "y": 0, "w": 0, "h": 0},
      "start_time": 0.0,
      "end_time": 0.0,
      "source_text": "string",
      "target_text": "string - 仅 translate 时填写英文翻译",
      "source_text_removal": true,
      "removal_method": ["inpaint", "clone_patch", "crop"],
      "replacement": {
        "asset": "input/brand-logo.png",
        "fit": "contain",
        "scale": "auto",
        "position": "bbox_center",
        "padding": 0,
        "opacity": 1.0
      },
      "reason": "string - 判断理由（基于证据）"
    }
  ],
  "audio_strategy": "mute_original_tts_bgm|keep_original",
  "bgm": {
    "asset_source": "user_input|local_library|none",
    "asset_path": "string",
    "style_hint": "string"
  }
}
```

## Few-shot 示例
**输入**：
- Track t1: "科比完成了绝杀"，底部，3.2-6.8s，asr_text_overlap=0.78
- Track t2: "某体育"，右上角，0-92.4s，固定
- Track t3: "某品牌广告牌"，背景，41-44.5s，不确定是否是场边实景

**输出**：
```json
{
  "items": [
    {
      "item_id": "t1",
      "category": "spoken_subtitle",
      "action": "translate",
      "confidence": 0.96,
      "bbox": {"x": 100, "y": 1600, "w": 880, "h": 90},
      "start_time": 3.2,
      "end_time": 6.8,
      "source_text": "科比完成了绝杀",
      "target_text": "Kobe hit the game-winning shot.",
      "source_text_removal": true,
      "removal_method": ["inpaint", "clone_patch"],
      "replacement": null,
      "reason": "底部位置+短持续+与ASR词汇重合高(0.78)，确定为口播字幕"
    },
    {
      "item_id": "t2",
      "category": "logo",
      "action": "replace",
      "confidence": 0.93,
      "bbox": {"x": 850, "y": 50, "w": 180, "h": 60},
      "start_time": 0.0,
      "end_time": 92.4,
      "source_text": "某体育",
      "target_text": null,
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
      "reason": "右上角+全片固定存在→品牌Logo；用户提供了英文Logo资产"
    },
    {
      "item_id": "t3",
      "category": "ad",
      "action": "review",
      "confidence": 0.55,
      "bbox": {"x": 200, "y": 900, "w": 300, "h": 120},
      "start_time": 41.0,
      "end_time": 44.5,
      "source_text": "某品牌广告牌",
      "target_text": null,
      "source_text_removal": false,
      "removal_method": [],
      "replacement": null,
      "reason": "背景广告牌，可能是场边实景的一部分，删除风险高，进入review"
    }
  ],
  "audio_strategy": "mute_original_tts_bgm",
  "bgm": {"asset_source": "none", "asset_path": null, "style_hint": "epic"}
}
```
