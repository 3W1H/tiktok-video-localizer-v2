# Case 3: 水印 remove + Logo replace

## 测试场景
视频包含中文水印和中文 Logo，需要：
- 水印：移除（inpaint）
- Logo：替换为用户提供的英文 Logo

## 输入
- `source.mp4`：含右上角中文 Logo（全片固定）+ 右下角水印
- `brand-logo.png`：用户英文 Logo 资产
- 无 bgm.mp3

## 预期结果
1. **分类**：
   - 右上角固定 Logo → `category: logo`, `action: replace`
   - 右下角水印 → `category: watermark`, `action: remove`
2. **Logo 替换**：
   - 原中文 Logo 被 inpaint 移除
   - 用户 brand-logo.png 被贴合到原 Logo 位置
   - **fit: contain**（保持宽高比，禁止拉伸变形）
   - 支持 padding、opacity
3. **水印移除**：OpenCV inpaint 清理
4. **QA**：
   - 检查 replace 后 Logo 可见性
   - 水印区复检
   - 残留中文 OCR 抽检

## 验证点
- [ ] Logo 和 watermark 是不同 category，不混用逻辑
- [ ] Logo 替换后不变形（contain 保持宽高比）
- [ ] 水印被清除
- [ ] clean-report 如实标注 LOW confidence 区域
- [ ] 如果 brand-logo.png 缺失，脚本退出码 1 拒绝，不静默失败

## 预期 localization-plan 片段
```json
{
  "items": [
    {
      "item_id": "t1",
      "category": "logo",
      "action": "replace",
      "confidence": 0.93,
      "bbox": {"x": 850, "y": 50, "w": 180, "h": 60},
      "start_time": 0,
      "end_time": 60,
      "source_text_removal": true,
      "removal_method": ["inpaint"],
      "replacement": {
        "asset": "input/brand-logo.png",
        "fit": "contain",
        "scale": "auto",
        "position": "bbox_center",
        "padding": 0,
        "opacity": 1.0
      }
    },
    {
      "item_id": "t2",
      "category": "watermark",
      "action": "remove",
      "confidence": 0.91,
      "bbox": {"x": 900, "y": 1850, "w": 150, "h": 50},
      "start_time": 0,
      "end_time": 60
    }
  ]
}
```
