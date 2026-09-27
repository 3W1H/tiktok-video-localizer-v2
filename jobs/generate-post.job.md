# Job: generate-post

## 目标
生成 TikTok 发布方案 tiktok-post.md（九段结构 Markdown）。

## 输入
- `work/video-understanding.json`
- `work/localization-plan.json`
- `work/cover-plan.json`
- `work/audio-plan.json`
- `work/qa.json`（QA 结果，用于第八段如实披露）
- `work/post-content.json`（Agent 产出的文案，可选）
- `configs/bgm-library.yaml` + `configs/bgm-library-added.json`（署名行反查用）
- `profiles/{profile}.yaml`（post_style 段）

## 输出
- `output/tiktok-post.md`（九段结构）

## 九段结构（以 `generate_post.py::render_markdown` 的实际顺序为准）
1. **Video Summary**：主题 + 简介
2. **TikTok Caption**：Hook + Context + Emotion
   —— ⭐ 紧跟 `Music: <曲名> by <作者> (<来源>), licensed under CC BY X.0` **署名行**
   （CC BY 的 "BY" 是许可条件，见 SKILL.md §12.4 第 6 条；脚本自动生成）
3. **Hashtags**：≤5 个，Relevance > Quantity
4. **Pinned Comment**：开放问题 / 二选一 / 争论式
5. **BGM Recommendations**：首选 + 3 备选，标注 Trending/StyleMatch/Licensing
6. **Posting Time**：北京时间 + 美国时间（美东/美西）
7. **Video Cover**：Cover 文案 + 设计说明
8. **Processing Report**：如实披露降级/低置信度/未解决项
9. **Final Files**：四件套清单

## 步骤

### Step 1: [AGENT] 生成 Caption
- 使用 `prompts/tiktok_caption.md`
- 输入：video-understanding + profile
- 产出：caption（Hook + Context + Emotion + CTA）

### Step 2: [AGENT] 生成 Hashtags
- 使用 `prompts/hashtag.md`
- 输入：video-understanding + profile hashtag 规则 + 可选趋势信息
- 产出：≤5 个标签，Relevance > Quantity

### Step 3: [AGENT] 生成 Pinned Comment
- 使用 `prompts/pinned_comment.md`
- 输入：video-understanding + profile
- 产出：开放问题 / 二选一 / 争论式评论

### Step 4: [AGENT] BGM 推荐
- 使用 `prompts/music_recommendation.md`
- 输入：video-understanding（情绪/BPM/高潮）+ profile bgm_style + 可选 WebSearch 趋势
- 产出：首选 + 3 备选，标注 Trending/StyleMatch/Licensing
- **重要**：BGM 推荐 ≠ BGM Asset，推荐曲目标注授权状态，热门不等于可商用

### Step 5: 拼装 tiktok-post.md
```bash
python scripts/generate_post.py --work-dir work --output-dir output --profile {profile}
```
- 按模板拼装九段结构
- 第八段「Processing Report」如实披露：
  - QA overall status（PASS/WARNING/FAIL）
  - 能力降级记录（degradations）
  - 低置信度保留项（review_items）
  - timeline 未解决项
  - 清理低置信度区域
- 产出：`output/tiktok-post.md`

## 规则
- 最终用户输出是 Markdown，不是 JSON
- 不要让用户为了使用结果去处理 JSON
- 第七段必须如实披露，禁止假装成功
- BGM 推荐段落必须区分"已混入的 Asset"与"仅推荐曲目"
- **⚠️ `post-content.json` 缺失 = 静默套通用模板（2026-09-27 踩坑，必读）**：
  `generate_post.py` 找不到 `work/post-content.json` 时**不会报错**，它会套一段
  与视频无关的通用英文模板 —— 出片后 `tiktok-post.md` 格式完整、语法正确，
  极易被当成"已完成"直接交付。**本步骤的默认姿势是 Agent 手写 `post-content.json`
  （caption / hashtags / pinned comment / headline / caption 全给全），脚本只做拼装。**
- **交付前必读一遍 `tiktok-post.md` 正文**：检查是否出现本条视频的**具体**人名 / 事件 / 比分 /
  看点；凡只有通用感叹句（"what a play!" 一类）→ 判定为模板兜底，回去补 content。
- Caption / Hashtags 一律**英文**（全英文输出，带 Part 序号编排，见 SKILL.md §6 / §12.7）。
