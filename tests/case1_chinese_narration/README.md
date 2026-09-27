# Case 1: 中文旁白 → 静音 + TTS + BGM

## 测试场景
视频包含中文解说旁白，需要本地化为英文配音 + 英文字幕 + BGM。

## 输入
- `source.mp4`：60秒视频，含中文解说
- 无 brand-logo.png
- 无 bgm.mp3（使用本地曲库或无 BGM）

## 预期结果
1. **ASR**：检测到 `has_chinese_narration: true`
2. **音频策略**：`mute_original_tts_bgm`
3. **原音轨**：被静音
4. **TTS**：生成英文配音，按 timeline 时间轴放置
5. **字幕**：生成英文字幕 SRT，烧录到视频
6. **BGM**：如果有本地曲库，混入 BGM；否则纯 TTS
7. **QA**：PASS 或 WARNING（如果 TTS/BGM 降级）

## 验证点
- [ ] final.mp4 无中文人声残留
- [ ] 英文字幕与语音同步
- [ ] 字幕时间轴以原视频为主约束
- [ ] tiktok-post.md 第七段如实披露音频策略
- [ ] 如果 TTS 不可用，QA WARNING 且视频仍有字幕

## 预期 localization-plan 片段
```json
{
  "audio_strategy": "mute_original_tts_bgm",
  "items": [
    {
      "category": "spoken_subtitle",
      "action": "translate",
      "source_text_removal": true,
      "confidence": 0.95
    }
  ]
}
```
