# `batch/` — 全批交付编排工具包（参考实现）

来源：2026-11-13～15 的 42 条 Downloads 批次。这一层**不是单视频 pipeline**，
而是在 `scripts/run_pipeline.py` 之上做「多视频排队 / 幂等续跑 / 全批自查 / 磁盘守护」。
设计规则与事故复盘统一写在 `SKILL.md` §17，本文件只讲怎么跑。

> ⚠️ 这些脚本已从原批次目录复制并**参数化**。原批次里的常量是硬编码绝对路径，
> 这里全部改为环境变量推导。**运行前必须设置 `LOCALIZER_BATCH_ROOT`**，否则会在
> `batch/` 目录下找 `diag/index.json` 而失败。

## 1. 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LOCALIZER_BATCH_ROOT` | 本文件所在目录 | 批次工作目录，需含 `diag/index.json`、`logs/`、`diag/` |
| `LOCALIZER_SKILL_ROOT` | `batch/` 的父目录（即 skill 根） | 用来定位 `scripts/`、`processed-videos.json` |
| `LOCALIZER_OUT_ROOT` | `~/Downloads/tiktok` | 每条视频的输出目录父级 |
| `LOCALIZER_PYTHON` | `sys.executable` | 解释器（需 cv2 / PIL / numpy / yaml / rapidocr） |
| `LOCALIZER_BIN_DIR` | `dirname(PY)` | 追加到 `PATH` 前面，ffmpeg/ffprobe 可放这里 |
| `LOCALIZER_MUSIC_LIB` | `<skill>/music/sports` | 免版权曲库（`bgm_review.py` 的白名单） |
| `LOCALIZER_HARNESS` | 空 | `process_one.py --phase digest` 用的 `build_semantics.py` 所在目录；不跑该阶段可不设 |

`ffmpeg` / `ffprobe` 默认从 `~/.local/bin` 找；`PYTHON` 用 conda 环境（本机是
`/Users/jason/opt/miniconda3/bin/python`）。

## 2. 前置输入

`diag/index.json` 是本批次的唯一真源：每条记录至少含 `idx` 与 `path`（源视频路径）。
**中文文件名一律通过 idx 索引读取，绝不手打**（SKILL.md §13.11 规则 2）。
`paths(idx)` 会容忍用户手动给输出目录加的 `-done` 后缀。

## 3. 典型流程

```bash
export LOCALIZER_BATCH_ROOT=/path/to/my-batch
export LOCALIZER_SKILL_ROOT=/path/to/tiktok-video-localizer-v2
export LOCALIZER_OUT_ROOT=~/Downloads/tiktok
export LOCALIZER_PYTHON=/path/to/conda/bin/python

# 1) 逐条取证 + 决策（erase-only 清理项、BGM 决策树、逐条文案）
python batch/audio_policy.py --idx 3          # -> work/audio-policy.json
python batch/auto_decide.py --idx 3           # -> work/agent-decisions.json
python batch/rewrite_posts.py --export        # -> diag/post-copy.json（逐条文案真源）

# 2) 幂等队列（可随时中断，重跑同一命令即续跑）
python batch/run_queue.py --limit 5
python batch/run_queue.py --only 3,7
python batch/run_queue.py --force            # 强制重渲（v1 糊带返工用）

# 3) 磁盘守护只在批量渲染期间显式常驻
python batch/disk_guard.py --keep-alive --interval 240 &   # 收尾后务必 kill

# 4) 收尾自查
python batch/verify_all.py --json diag/final-verification.json
```

### 双 worker（4 核机器实测最优）

按奇偶 idx 分片各起一个 `run_queue.py` 实例，所有共享 JSON 的读改写都经
`fcntl.flock` 的 `shared_lock()`（status / ledger / failures），避免互相覆盖。
4 核 8 线程上 2 路已接近饱和，再加并发只会更慢。

## 4. 各脚本职责

| 脚本 | 职责 |
|---|---|
| `run_queue.py` | 幂等队列、`is_delivered()` 跳过、`repair_from_qa()` 补框、状态/台账/失败记录 |
| `process_one.py` | 单条 analyze / digest / render / qa 四阶段编排；批量快速路径 `write_fast_masks()` |
| `auto_decide.py` | 从 OCR 生成 erase-only 清理项（v2 连续时间段 + 逐文字小框）、BGM 决策、封面文案 |
| `audio_policy.py` | BGM 决策树（只给建议，不自动换曲、不自动静音） |
| `bgm_review.py` | 仅在「无解说无 BGM」时评审本地曲库；含曲库分析缓存 |
| `rewrite_posts.py` | **逐条英文文案真源**；`--export` 产出 `diag/post-copy.json`，`auto_decide.py` 按 idx 读取 |
| `verify_all.py` | 全批规则级自查（四件套 / 中文残留 / R1 画面零新增文字 / R2 BGM 决策树 / R3 封面品牌黄 / 台账） |
| `disk_guard.py` | 回收已交付条目的中间 PNG 与冗余 `cleaned.mp4`；**全批完成即自退**，只有 `--keep-alive` 才常驻 |
| `finalize_when_done.py` | 等两路 worker 打出 `QUEUE END` 后补跑未完成条目、刷新 QA、跑自查、生成 `FINAL-REPORT.md` |

## 5. 新批次必须重写的部分

以下几处是 2026-11-13 批次**专用数据**，不能直接套用到新视频：

- `auto_decide.py` 的 `COVER` 表（封面 headline 按 idx 手写）。
- `rewrite_posts.py` 的 `COPY` 表（36 条逐条撰写的 Summary / Caption / 5 个 Hashtag / Pinned Comment）。
- `verify_all.py` 的 `RULE_MAP` 若 BGM 策略有新增，需同步。
- `diag/index.json`、`diag/post-copy.json` 必须按新批次重新生成。

**文案纪律**：禁止用批量模板生成 `tiktok-post.md`，只换 headline 会让几十条文案结构完全相同。
事实来源优先级是 `源文件名 > 该条 OCR/ASR > cover headline`（COVER 是人工映射，可能写错）。
改文案后用 skill 自带 `scripts/generate_post.py` 重渲九段结构，不要手改 md。

## 6. 已知边界

- 批量模式用自动化替代了逐条人工目视决策，**图形化中文、非中文 logo、贴纸**这类
  OCR 漏检项无法自动发现；QA 的中文残留 OCR 硬门禁仍逐条跑，但**查不到擦除痕迹**，
  所以必须另做抽帧目视复核（见 §17.9）。
- 亮色描边中文艺术字压在复杂/暗背景上，本机没有生成式 video inpainting，
  整框 TELEA / vblend 必然留一层低频渐变。切块 TELEA 会产生彩色菱形斑，
  笔画 mask 会漏黑色描边，实测都不如现状。判定顺序恒为：**先目视确认画面干净，
  再调整门禁**，绝不先放宽标准再看结果。
- 不要为提速改 x264 preset：`ultrafast` 快 3 倍但体积膨胀 3.3 倍会撑爆磁盘，
  保持 `veryfast` + CRF18 + `--x264-threads 4`。
- 常驻守护一律「随任务起、随任务停」；收尾后 `pgrep` 复查，不留 resident 进程。
