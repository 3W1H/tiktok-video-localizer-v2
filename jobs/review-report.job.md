# Job: review-report

## 目标
汇总低置信度（action=review）项，生成人类可读报告，交用户裁决。

## 触发条件
- 用户使用 `--review` 参数
- 或 localization-plan.json 中存在 action=review 的项（confidence < 0.60）

## 输入
- `work/localization-plan.json`（action=review 的项）
- `work/text-tracks.json`（完整证据包）
- `work/video-understanding.json`

## 输出
- `work/review-report.json`（结构化）
- 向用户展示的 Markdown 报告

## 步骤

### Step 1: [AGENT] 生成 Review 报告
- 使用 `prompts/review_report.md`
- 输入：localization-plan 中 action=review 的项 + text-tracks 证据包 + video-understanding
- 每项包含：
  - 编号
  - 截图时间点（start_time）
  - 区域描述（位置 + 文本内容）
  - 当前判断（category + 为什么不确定）
  - 建议处理方式（keep/remove/translate/replace）
  - 风险提示（删除可能的后果）
- 产出：`work/review-report.json`

### Step 2: 向用户展示报告
- 展示 Markdown 格式报告
- 说明：发现 N 个低置信度区域，建议人工确认
- 提供用户操作指引：回复编号 + 动作，或全部接受建议

### Step 3: 等待用户裁决
- 用户回复后，更新 localization-plan.json
- 将 action=review 的项改为用户指定的 action（keep/remove/translate/replace）
- 更新 confidence 和 reason

### Step 4: 继续后续流程
- 用户裁决后，继续 localize-video job
- 未裁决的项默认保留（keep）+ QA WARNING

## 规则
- **宁可保留+Warning，不可误删**：confidence < 0.60 一律 review
- 客观描述：只陈述事实和证据，不替用户做最终决定
- 风险提示：明确说明删除/保留各自的风险
- 禁止使用"肯定是"、"一定"等绝对化表述
- --review 模式下所有 replace/remove 项先出报告再执行
