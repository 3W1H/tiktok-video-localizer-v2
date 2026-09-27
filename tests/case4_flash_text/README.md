# Case 4: 0.5s 闪现文字 → 四层采样 + Tracking

## 测试场景
视频中有只出现 0.5 秒的中文快闪标题，验证四层自适应采样和文字跟踪能否捕获。

## 输入
- `source.mp4`：30秒视频，第 10.0-10.5 秒出现中央中文标题"绝杀时刻"
- 其他时间无文字

## 预期结果
1. **第一层粗采样（1FPS）**：可能错过 0.5 秒闪现（10.0s 和 11.0s 之间）
2. **第二层场景变化**：如果闪现伴随场景切换，可能捕获
3. **第三层局部加密**：如果第一遍 OCR 检测到疑似文字窗口，对该窗口加密采样（5FPS），应该能捕获
4. **第四层确认时间**：track_text.py 确定精确的 start_time/end_time
5. **Text Tracking**：
   - `persistence: flash`（< 1秒）
   - `position: center`
   - `trajectory: static`
   - 精确的 start_time ≈ 10.0, end_time ≈ 10.5

## 验证点
- [ ] 0.5 秒闪现文字被检测到（不是全局 5FPS，而是局部加密）
- [ ] track_text 输出 persistence=flash
- [ ] start_time/end_time 精确（误差 < 0.3 秒）
- [ ] 计算成本可控（只加密疑似窗口，不是全视频高帧率）

## 预期 text-tracks 片段
```json
{
  "tracks": [
    {
      "track_id": "t1",
      "text": "绝杀时刻",
      "language": "zh",
      "start_time": 10.0,
      "end_time": 10.5,
      "duration_sec": 0.5,
      "position": "center",
      "persistence": "flash",
      "trajectory": "static",
      "confidence": 0.85
    }
  ]
}
```
