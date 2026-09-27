# Case 5: 低置信度 → 进入 Review，不被删除

## 测试场景
视频中有不确定的中文区域（背景广告牌、模糊文字），confidence < 0.60，验证系统是否进入 review 模式而不是误删。

## 输入
- `source.mp4`：含背景中文字（可能是场边广告牌的一部分），文字模糊且位置不固定
- `--review` 参数启用

## 预期结果
1. **分类**：
   - 不确定区域 → `confidence < 0.60`
   - `action: review`（不执行删除）
2. **Review 报告**：
   - 向用户输出报告（编号+时间点+区域描述+建议）
   - 每项给出建议处理方式和风险提示
3. **clean_video**：
   - **拒绝执行 action=review 的删除**
   - 不视为失败
   - 该区域保留原样
4. **QA**：
   - review_items_pending > 0
   - overall = WARNING
   - tiktok-post.md 第七段如实披露

## 验证点
- [ ] confidence < 0.60 的项 action=review，不被删除
- [ ] clean_video 跳过 review 项，不报错
- [ ] review-report.md 生成人类可读报告
- [ ] QA WARNING 包含 review 项
- [ ] tiktok-post.md 第七段如实披露低置信度保留项
- [ ] 宁可保留+Warning，不可误删

## 预期 localization-plan 片段
```json
{
  "items": [
    {
      "item_id": "t1",
      "category": "ad",
      "action": "review",
      "confidence": 0.45,
      "bbox": {"x": 200, "y": 900, "w": 300, "h": 120},
      "start_time": 41.0,
      "end_time": 44.5,
      "source_text": "某品牌广告牌",
      "reason": "背景广告牌，可能是场边实景的一部分，删除风险高"
    }
  ]
}
```

## 预期 qa.json 片段
```json
{
  "overall": "WARNING",
  "checks": {
    "localization": {
      "status": "warning",
      "review_items_pending": 1
    }
  },
  "review_items": [
    {"item_id": "t1", "summary": "背景广告牌(00:41)，置信度0.45，已保留原样"}
  ],
  "confidence_summary": {"auto": 0, "warning_band": 0, "review": 1}
}
```
