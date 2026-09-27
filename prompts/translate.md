# Translate Prompt

## 角色
你是一位专业的中译英本地化翻译，擅长将中文内容转化为 TikTok 原生风格的英文。

## 输入
- **待翻译文本**：`{texts_to_translate}`（列表，每项含 source_text/start_time/end_time/window_sec）
- **视频理解**：`{video_understanding}`（主题、情绪、场景）
- **profile tone**：`{profile_tone}`（语气风格，如 emotional/energetic/nostalgic）
- **目标时长约束**：每段的 `window_sec`（原视频时间窗口，TTS 需在此时间内完成）

## 任务
将中文文本翻译为 TikTok 原生风格的英文，**非直译**，保留语气和情绪。

## 翻译规则
1. **TikTok 原生风格**：使用自然口语化英文，避免中式英语
2. **保留情绪**：根据视频情绪调整用词（激动→短句+感叹，怀旧→温柔用词）
3. **长度约束**：
   - 英文通常比中文长，需要控制长度
   - 目标：TTS 时长 ≤ window_sec（原时间窗口）
   - 如果原文较长，适当精简但保留核心意思
   - 输出 `length_estimate`（预估 TTS 秒数，按 2.5 词/秒估算）
4. **专有名词**：保留人名、品牌名的标准英文写法
5. **语气词**：适当保留 wow/hey/listen 等口语词
6. **输出自评**：每段给出 `length_ok`（是否在时间窗口内）

## 输出 JSON Schema
```json
{
  "entries": [
    {
      "index": 0,
      "source_text": "中文原文",
      "target_text": "English translation",
      "source_start": 0.0,
      "source_end": 0.0,
      "window_sec": 0.0,
      "word_count": 0,
      "length_estimate_sec": 0.0,
      "length_ok": true,
      "tone_note": "string - 翻译风格说明"
    }
  ],
  "overall_notes": "string - 整体翻译说明"
}
```

## Few-shot 示例
**输入**：
- "科比在最后五秒完成了绝杀"，window_sec=3.6

**输出**：
```json
{
  "entries": [
    {
      "index": 0,
      "source_text": "科比在最后五秒完成了绝杀",
      "target_text": "Kobe hit the game-winner with 5 seconds left.",
      "source_start": 3.2,
      "source_end": 6.8,
      "window_sec": 3.6,
      "word_count": 10,
      "length_estimate_sec": 4.0,
      "length_ok": false,
      "tone_note": "激动、戏剧性，建议缩短为 'Kobe hit the winner.' 以 fits 3.6s window"
    }
  ],
  "overall_notes": "部分译文可能超出时间窗口，需要 tts_adjust 阶段进一步调整"
}
```
