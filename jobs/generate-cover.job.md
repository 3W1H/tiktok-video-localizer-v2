# Job: generate-cover

## 目标
生成 TikTok 封面（1080×1920，9:16），Profile 驱动样式。

**排版与绘制由 `scripts/cover_engine.py` 统一承担** —— 与手动/预览用的
`make_cover.py` 是同一个引擎。本 job 的脚本只负责选帧、按 plan 清帧、组装
CoverSpec、落盘。（2026-09-27 之前这里另有一套渲染器，标题画在安全区之外，
是「流水线出的封面跟手调的不一样」的根源。）

## 输入
- `work/frames-index.json`
- `work/video-understanding.json`（Agent 产出）
- `work/cover-plan.json`（Agent 选帧+文案，可选）
- `work/localization-plan.json`（封面帧按它清中文/水印）
- `profiles/{profile}.yaml`（cover_style 段）

## 输出
- `work/cover-plan.json`
- `output/cover.jpg`

## 步骤

### Step 1: [AGENT] 候选帧筛选
- 输入：frames-index.json + video-understanding.json（highlight_timestamps）
- 首选 `python scripts/pick_cover_frame.py --work-dir work --contact work/scene_ranked.png`
  按 `face_area*1000 - clutter*420 + lap` 排名，**并肉眼确认主体是谁**
  （分数只用来筛候选，最高分可能是背影）
- 不要选远景全景：整片观众席无论怎么后期都救不回来（用户两次反馈「画面很花」）
- 产出：候选帧列表 / `cover-plan.json` 的 `selected_frame`

### Step 2: [AGENT] Cover 文案
- 使用 `prompts/cover_copy.md`
- 产出 `cover-plan.json` 的文案字段（缺省会被脚本跳过，不影响出图）：

  | 字段 | 用途 | 示例 |
  |---|---|---|
  | `headline` | 主标题（金，安全带顶部） | `ALLEN IVERSON` |
  | `sub` | 灰副标（可选） | `2001 NBA FINALS - GAME 1` |
  | `stat` | 底部数据行（金，与主标题同号） | `48 POINTS` |
  | `tagline` | 白 tagline（可选） | `SIXERS SHOCK THE LAKERS` |

- 全大写，高冲击力；品牌署名 `Hoop Dynasty Daily` 由 profile.brand_text 自动落在底部
- 可用 CLI 覆盖：`--headline/--sub/--stat/--tagline`
- **本步是 Step 3 渲染的前置**：必须先写完正式文案再渲染，不做「占位标题先出图」
  （封面优先只表示排在 localize 之前，不表示跳过文案；占位版后续必返工）

### Step 3: 渲染封面（Profile 驱动）
```bash
python scripts/generate_cover.py --work-dir work --output-dir output --profile {profile}
```
- 样式全部取自 `profile.cover_style`（`font_chain` 字体、`font_size` 主标题、
  `brand_size` 品牌、`fill_color` 颜色、`safe_zone_*_pct` 安全区、`picture_mode`）
- **帧清理**：按 localization-plan 的 remove/translate 项，大区域高斯模糊+黑遮罩、
  小区域 OpenCV inpaint；封面比视频清得更重（放大后才不会露出中文）
- **排版**：主标题 / 数据行 / 品牌署名**全部落在安全区**
  （默认中心 1080×1080，`safe_zone_*_pct = 0.22`），主页网格裁切后仍完整可见
- 脚本会打印 `safe-zone: composition A-B inside C-D -> OK|OVERFLOW`，
  **OVERFLOW 就是不合格**
- 产出：`output/cover.jpg` + `work/cover-plan.json`（含 `render.text_regions` /
  `brand_region` / `safe_text_zone` / `picture_src`，供 `qa_video.py::check_cover` 复核）

### Step 4（可选）: 用户自带封面图
```bash
python scripts/generate_cover.py --work-dir work --output-dir output --profile {profile} \
    --cover-image /path/to/cover.png [--no-resize] [--no-text] [--bottom-text "..."]
```
- 只做「按画幅适配 + 叠加标题/品牌」，**不改动用户的画面**
- 文字同样落在安全区内（旧实现画在高度 4% 处，会被主页网格裁掉）

## 规则
- Cover Style 属于 Profile，不写死模板
- 新增垂类（NFL/Soccer/Motivation）只需加 YAML，`cover_engine.py` 零改动
- 默认 1080×1920，9:16
- NBA profile 定稿值：Impact 127px 主标题 / Impact 64px 品牌 / 黄 #FFDC00 /
  安全区 0.22-0.22 / 画面 `fill`
- 两个封面入口（`generate_cover.py` 流水线、`make_cover.py` 手工）必须同源，
  改渲染逻辑只改 `cover_engine.py`
