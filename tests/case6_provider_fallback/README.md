# Case 6: Provider 不可用 → 自动降级 + 如实上报

## 测试场景
模拟 ASR/TTS/OCR 等 AI 能力不可用，验证系统是否自动降级并如实上报，而不是崩溃或假装成功。

## 输入
- `source.mp4`：普通中文视频
- 环境：未安装 whisper、未安装 paddleocr/tesseract、无 TTS CLI

## 预期结果

### ASR 不可用
1. `asr.json`：`degraded: true`, `provider_used: "none"`
2. 音频策略：保守走 `keep_original`（宁可保留原声也不误静音）
3. QA WARNING："未做语音转写"
4. 字幕：仅来自 OCR 翻译（如果 OCR 可用）

### TTS 不可用
1. `tts-result.json`：`degraded: true`, `provider_used: "none"`
2. 有中文旁白但无 TTS：静音原轨 + 无配音 + 英文字幕 + BGM
3. QA WARNING
4. 视频仍可用（靠字幕传达）

### OCR 不可用
1. `ocr.json`：`degraded: true`, `ocr_engine: "none"`
2. 文字类处理全部跳过
3. 仅走 ASR 字幕翻译路径
4. QA WARNING："画面文字未处理"

### Vision 不可用
1. `video-understanding.json`：`degraded: true`, `basis: "ocr_asr_only"`
2. 分类照常（证据包不依赖 Vision）
3. caption/cover 文案质量下降
4. QA WARNING

### WebSearch 不可用
1. BGM 推荐：仅按 profile bgm_style 从本地曲库选择
2. 推荐段落标注"未获取趋势数据"
3. **绝不虚构 Trending 属性**

## 验证点
- [ ] 任何降级写入 `provider_used` + `degraded` 字段
- [ ] `qa.json` 汇总所有 degradations
- [ ] tiktok-post.md 第七段如实披露降级
- [ ] 系统不崩溃，产物仍生成
- [ ] 不假装成功（不谎称已清除/已翻译）
- [ ] ASR 不可用时保守 keep_original（不误静音）
- [ ] WebSearch 不可用时不虚构 Trending

## 预期 qa.json 片段
```json
{
  "overall": "WARNING",
  "degradations": [
    {"capability": "asr", "used": "none", "reason": "No ASR engine available"},
    {"capability": "tts", "used": "none", "reason": "No TTS engine available"},
    {"capability": "ocr", "used": "none", "reason": "No OCR engine available"}
  ],
  "checks": {
    "localization": {
      "status": "warning",
      "detail": "Multiple capability degradations, text processing limited"
    }
  }
}
```

## 通用兜底原则
1. 任何降级必须写入对应 JSON 的 provider_used + degraded 字段
2. 不确定时宁可保留 + Warning，不可误删
3. 所有 fallback 终点都是"产物仍生成 + 如实披露"，而非中断或假装成功
4. 确定性工具（FFmpeg/OpenCV/Pillow）缺失属于环境问题：直接报错退出（码 2），不做 fallback
