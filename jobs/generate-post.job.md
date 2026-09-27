# Job: generate-post

## 目标
生成 TikTok 发布方案 tiktok-post.md（八段结构 Markdown）。

## 输入
- `work/video-understanding.json`
- `work/localization-plan.json`
- `work/cover-plan.json`
- `work/audio-plan.json`
- `work/qa.json`（QA 结果，用于第七段如实披露）
- `work/post-content.json`（Agent 产出的文案，可选）
- `profiles/{profile}.yaml`（post_style 段）

## 输出
- `output/tiktok-post.md`（八段结构）

## 八段结构
1. **视频内容简介**：主题 + 简介
2. **TikTok Caption**：Hook + Context + Emotion
3. **话题标签**：≤5 个，Relevance > Quantity
4. **推荐置顶评论**：开放问题 / 二选一 / 争论式
5. **推荐背景音乐**：首选 + 3 备选，标注 Trending/StyleMatch/Licensing
6. **视频封面**：Cover 文案 + 设计说明
7. **视频处理结果**：如实披露降级/低置信度/未解决项
8. **最终文件**：四件套清单

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
- 按模板拼装八段结构
- 第七段「视频处理结果」如实披露：
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
