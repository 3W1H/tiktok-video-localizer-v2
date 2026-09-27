# Case 2: 无中文旁白 → 保留原声

## 测试场景
视频无中文解说（只有现场声、欢呼声、环境音），不需要 TTS。

## 输入
- `source.mp4`：60秒比赛现场视频，只有现场声音，无中文旁白
- 无 brand-logo.png
- 无 bgm.mp3

## 预期结果
1. **ASR**：检测到 `has_chinese_narration: false`（或 ASR 无中文段）
2. **音频策略**：`keep_original`
3. **原音轨**：完整保留（现场声/欢呼/环境音）
4. **TTS**：不生成（无中文旁白需要翻译）
5. **字幕**：如果画面有中文字幕，翻译并烧录；否则无字幕
6. **BGM**：不混入（保留原声）
7. **QA**：PASS

## 验证点
- [ ] final.mp4 保留原始现场声音
- [ ] 无英文 TTS 配音
- [ ] 无额外 BGM 混入
- [ ] 如果 ASR 不可用，保守走 keep_original + WARNING

## 预期 localization-plan 片段
```json
{
  "audio_strategy": "keep_original",
  "items": []
}
```
