# Troubleshooting - 常见问题与降级处理

## 环境问题

### ffmpeg 命令"永远不结束"（抽帧/转码卡死）
**症状**：脚本停在 `抽帧中…` 不动，`timeout` 杀掉才退出；同一条 ffmpeg 命令在终端手跑
却 1 秒完成。用 `sample <pid>` 采样可见进程阻塞在 `subprocess.communicate()`。

**原因**：ffmpeg 默认会读 stdin（交互按键）。当父进程由 Agent / cron / CI 拉起、stdin 是
一个没人写的管道时，ffmpeg 读 stdin 会**永久阻塞** —— 与磁盘、权限、视频文件都无关。

**解决**：`subprocess.run(..., stdin=subprocess.DEVNULL)`（`common.run()` 已默认这样），
或命令里加 `-nostdin`。手工排查时先跑
`ffmpeg -nostdin -y -v error -f lavfi -i testsrc=size=160x120:rate=1 -frames:v 1 /tmp/t.jpg`
—— 加 `-nostdin` 能出图，就能确诊是这个问题。

### `--output` 生效了，但 work 目录跑到了别处（重跑时「跳过一切」）
**症状**：对 `/tmp/xxx/clean.mp4` 这类**不在原始素材目录**的视频跑
`run_pipeline.py --input /tmp/xxx/clean.mp4 --output /Users/me/Downloads/tiktok/<folder>`，
日志里的 `Output:` 显示正确，日志末尾也列出 `✅ final.mp4`，但 `<folder>/work` 里
**一个分析产物都没有**；紧接着 `apply.py` 报 `MISSING work dir`，再跑一次则直接
`⏭ SKIP (already processed)`，成品仍是旧文件（mtime 没变）。

**原因**：`--work-dir` 的默认值是 `<视频所在目录>/tiktok/<视频名>/work`，
`--output` **只决定最终四件套落在哪**。所以中间产物写到了 `/tmp/xxx/tiktok/...`，
`<folder>/work` 从未被创建；而 processed-log 已经记了这个输入，后续 run 全部跳过。

**解决**：显式给 `--work-dir`：
```bash
run_pipeline.py --input /tmp/xxx/clean.mp4 \
  --work-dir /Users/me/Downloads/tiktok/<folder>/work \
  --output   /Users/me/Downloads/tiktok/<folder> --resume --force
```
已经踩坑的话别重跑分析：把 `/tmp/xxx/tiktok/<name>/work` 整个 `cp -R` 到目标
`work/`，把 `pipeline-config.json` 里的 `work_dir` 改对，再 `apply.py` + 上面的命令
即可复用已有 ASR/OCR 结果。

### FFmpeg 未安装
**症状**：`video_probe.py` 或 `extract_frames.py` 报 `ffprobe/ffmpeg not found`
**解决**：
```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt-get install ffmpeg

# Windows
# 从 https://ffmpeg.org/download.html 下载并添加到 PATH
```

### Python 依赖缺失
**症状**：`ModuleNotFoundError: No module named 'cv2'` / `'PIL'` / `'yaml'`

**解决**：
```bash
pip install opencv-python pillow numpy pyyaml
```

### OCR 引擎未安装
**症状**：`ocr_video.py` 报 `RapidOCR (rapidocr-onnxruntime) 不可用`，`ocr.json` 中 `degraded: true`

**解决**（默认引擎，装这个就够）：
```bash
pip install rapidocr-onnxruntime
```

排障备选（正常流程不需要，仅用于对比/排查）：
```bash
# paddleocr：与 RapidOCR 共用同一套 PP-OCRv4 模型，只是运行时不同
pip install paddlepaddle paddleocr && python scripts/ocr_video.py --work-dir <dir> --engine paddleocr

# tesseract：对视频中文描边美术字召回≈0，禁止用于交付
pip install pytesseract && brew install tesseract tesseract-lang  # macOS
```

**降级行为**：OCR 不可用时，文字类处理全部跳过，仅走 ASR 字幕翻译路径，
QA 判 WARNING 而非 PASS。⚠️ **此时不得断言「成品无中文」** —— 那等于重演
2026-09-22 的假 PASS（C 视频残留中文被交付）。`chinese_residue` 检查会明确标记 warning。

### Whisper 未安装
**症状**：`audio_processor.py asr-only` 报 `No ASR engine available`

**解决**：
```bash
pip install openai-whisper
# 首次运行会自动下载 base 模型（约 150MB）
```

**降级行为**：ASR 不可用时，保守走 `keep_original`（宁可保留原声也不误静音），QA WARNING "未做语音转写"。

### TTS 引擎未安装
**症状**：`audio_processor.py tts` 报 `No TTS engine available`

**解决**（可选）：
```bash
pip install edge-tts
```

**降级行为**：TTS 不可用时，有中文旁白→静音原轨 + 无配音 + 英文字幕 + BGM（视频仍可用，靠字幕传达），QA WARNING。

## 处理质量问题

### 中文字幕未被移除（英文直接覆盖在中文上）
**症状**：final.mp4 中能看到原中文字幕，英文字幕盖在上面

**原因**：
1. localization-plan.json 中 translate 项的 `source_text_removal` 未设为 true
2. OCR 未检测到该字幕（文字太小/太快）
3. mask 生成失败

**解决**：
1. 检查 `work/localization-plan.json`，确保 translate 项有 `"source_text_removal": true`
2. 检查 `work/ocr.json`，确认该字幕被检测到
3. 检查 `work/masks/masks-index.json`，确认 mask 已生成
4. 检查 `work/clean-report.json`，确认清理执行

### Logo 替换后变形
**症状**：用户 Logo 被拉伸变形

**原因**：localization-plan.json 中 replacement.fit 未设为 "contain"

**解决**：确保 replacement 配置：
```json
"replacement": {
  "asset": "input/brand-logo.png",
  "fit": "contain",
  "scale": "auto",
  "position": "bbox_center",
  "padding": 0,
  "opacity": 1.0
}
```
`fit: contain` 会保持宽高比，完整放入原 Logo 区域，禁止拉伸变形。

### 清理后有残留/模糊
**症状**：水印或文字区域清理后有模糊痕迹

**原因**：
1. OpenCV inpaint 对复杂背景效果有限
2. 区域置信度低（< 0.7）

**解决**：
1. 检查 `work/clean-report.json` 中的 `low_confidence_count`
2. QA 会对水印区复检，残留→WARNING（不谎称已清除）
3. V1 不使用 AI inpainting（重型模型），如需更好效果可在 V2 启用
4. 对于低置信度区域，建议使用 `--review` 模式人工确认

### 字幕与语音不同步
**症状**：英文字幕出现时间与 TTS 语音不一致

**原因**：
1. timeline.json 未正确生成
2. TTS 超时未调整

**解决**：
1. 检查 `work/timing-report.json`，看是否有 overflow 项
2. 检查 `work/timing-decisions.json`，确认 Agent 已做调整决策
3. 检查 `work/timeline.json`，确认 final_start/final_end 合理
4. 字幕以 timeline.json 为准，不直接用 translated.json

### TTS 语速不自然
**症状**：英文配音听起来太快或太慢

**原因**：playback_rate 超出 [0.90, 1.15] 区间

**解决**：
- playback_rate 统一定义：1.0=原速，>1.0=加速（1.10=+10%），<1.0=降速
- 允许区间 [0.90, 1.15]，超过会明显不自然
- 优先用 shorten_translation（缩短译文）而不是 speed_up
- 检查 `work/timeline.json` 中每项的 adjustment.playback_rate

## 流程问题

### 断点续跑
**症状**：流程中断后重新运行，重复执行已完成步骤

**解决**：使用 `--resume` 参数：
```bash
python scripts/run_pipeline.py --input input/source.mp4 --work-dir work --output output --resume
```
脚本会检查中间产物是否存在，跳过已完成步骤。

### 低置信度项被误删
**症状**：重要内容（如场边广告牌、实景文字）被删除

**原因**：confidence 阈值设置不当，或未使用 --review 模式

**解决**：
1. 使用 `--review` 模式，所有 replace/remove 项先出报告再执行
2. confidence < 0.60 的项自动进入 review，不被删除
3. 检查 `work/qa.json` 中的 `review_items`，看哪些被保留
4. 宁可保留+Warning，不可误删

### QA FAIL 怎么办
**症状**：`qa.json` 中 overall = "FAIL"

**解决**：
1. 检查 `work/qa.json` 中哪个 check 是 fail
2. 定位失败环节，修复后重跑该 job（中间产物支持断点续跑）
3. 常见 FAIL 原因：
   - video: final.mp4 不存在或时长偏差过大
   - subtitle: subtitles-en.srt 不存在
   - cover: cover.jpg 不存在
   - audio: 无音频流
4. 仍失败则如实上报，不假装成功

## 性能问题

### 处理速度慢
**症状**：长视频处理时间过长

**优化**：
1. OCR 只在采样帧上做（不是全视频）
2. 加密采样仅限疑似文字窗口（不是全局 5FPS）
3. inpaint 优先 OpenCV（不是 AI inpainting）
4. Whisper 使用 base 模型（不是 large）
5. 检查 `configs/default.yaml` 中的采样参数

### 内存不足
**症状**：处理长视频时内存溢出

**优化**：
1. 减少 `max_total_frames`（configs/default.yaml）
2. 降低 `dense_fps`
3. 减少 `max_dense_window_sec`
4. 分段处理长视频

## BGM 问题

### 无 BGM 混入
**症状**：final.mp4 只有 TTS，没有 BGM

**原因**：
1. 用户未提供 input/bgm.mp3
2. 本地 music/ 曲库为空
3. audio-plan.json 中 bgm.asset_source = "none"

**解决**：
1. 提供 input/bgm.mp3
2. 或在 music/ 目录下放置音频文件（按风格分子目录）
3. 无 BGM 时静音原轨策略仍执行（纯 TTS），post.md 第五段如实写"未混入 BGM"
4. **V1 不自动下载商业音乐**

### BGM 推荐的歌曲不能用
**症状**：music_recommendation 推荐了热门歌曲，但无法获取音频文件

**说明**：
- BGM Recommendation ≠ BGM Asset
- 推荐只是建议，真正混入视频的必须是本地音频文件
- 热门不等于可商用，推荐曲目标注了 Licensing
- 发布前请确认授权状态
- V1 不下载商业音乐

## 诚实原则

### 如何判断是否"假装成功"
系统设计上禁止假装成功：
1. 任何降级写入 `provider_used` + `degraded` 字段
2. clean-report 如实标注 LOW confidence 区域
3. QA 残留中文抽检，残留→WARNING
4. tiktok-post.md 第七段必须如实披露
5. timeline unresolved → QA WARNING，不静默丢句
6. review 项保留原样，不被删除

**如果发现输出与实际不符**：
1. 检查 `work/qa.json` 的 overall 状态
2. 检查各中间 JSON 的 degraded 字段
3. 检查 tiktok-post.md 第七段
4. 如实告知用户，不掩盖问题

## 判定类事故（2026-11-13，音频策略 + 水印）

这两类都不是"脚本报错"，而是**判据写错却一路 PASS**，只能靠用户回头指出。
共同特征：**判定依据与决策后果不匹配**。

### 用户说"BGM 是垃圾 / 太单调"，但原片本来就没有音乐

**症状**：成品是整段循环的授权 BGM，听不出任何现场声/解说；用户连着几条反馈同一种感受。

**根因链（三层，缺一层就不会出事故）**：
1. `analyze_narration.py` 只判「有没有**中文**解说」；
2. job 在它答「否」后直接选 `remove_original_bgm_only`（丢整轨）；
3. 为了省时间，批处理把 ASR 标成 `skipped: batch uses remove_original_bgm_only`
   —— 实测 25 条里 **20 条**如此，于是**永远没有证据能反对这个决定**。

**排查**：
```bash
python scripts/analyze_source_audio.py --video input/source.mp4 --work-dir work
# 看 has_speech / speech_lang / has_music / recommended_strategy
```
它会自己跑 whisper 采样补证（3×45s），`asr.json` 被 skip 也不影响。

**修法**：按 `recommended_strategy` 走（SKILL.md §3.0 决策表）。
外语解说 → `keep_original`；原片自带 BGM → `keep_original`。
`remove_original_bgm_only` 只保留给「用户明确要求换原声」。

**别再犯的**：不要用「谱峰重合率 / 谱平坦度 / 高频能量占比」去区分音乐与人声 ——
实测 611s 英语解说的谱峰重合率 0.625，与电子乐完全相同。唯一可靠的是 ASR 语言检测。

### 有外语解说却判定「无解说」

**症状**：`asr.json` 里 `segments` 为空或全是乱码汉字；`has_chinese_narration=false`。

**根因**：ASR 曾把 `language="zh"` 钉死，英语音轨被强行按中文解码。

**排查**：看 `asr.json.language_detected`。若为 `unknown` 而片子明显有解说，
说明是老版产物（新版会写真实语言 + `has_foreign_narration`）。

**修法**：重跑 `audio_processor.py asr-only`（已改为自动检测语言）。

### 视频里还有水印，但 QA 判 PASS

**症状**：画面中部/角落有手写体英文 ID（`TONY-Zhong30`、`TMAC-Zhang30`），
`decisions.json` 里写着"拉丁字母不触发中文门禁，保留"。

**根因**：把「中文残留门禁」当成了「水印判据」。用户看的是"这像不像我的片子"，
不是"是不是汉字"。

**判据**：上传者签名/ID → 必须去；电视台标（TNT/ESPN）、自家品牌条 → 保留。

**做法**：`scripts/delogo_static.py`（2026-11-13 升级为中值 top-hat + 笔画级掩膜），
参数与验收标准见 SKILL.md「英文/手写水印」小节。**别用整框矩形 inpaint**，
那会在画面正中糊出一条横带。

