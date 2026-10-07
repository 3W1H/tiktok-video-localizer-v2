#!/usr/bin/env python3
"""
TikTok Video Localizer - 公共工具库

所有脚本共享的工具函数。
退出码约定：
  0 = EXIT_OK       成功
  1 = EXIT_DEGRADED 可降级失败（Agent 必须处理）
  2 = EXIT_FATAL    致命失败（环境/输入问题，直接上报）
"""

import json
import os
import re
import shutil
import subprocess
import sys
import logging
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# 退出码
# ============================================================
EXIT_OK = 0
EXIT_DEGRADED = 1
EXIT_FATAL = 2

# ============================================================
# 排名 / 倒计时叠字识别（共享，单一真源）
# ============================================================
# 篮球 / 体育集锦里常见的「排名 / 倒计时」叠字会承载「第几个球 / 第几名」语义信息，
# 例如：凯文加内特生涯十佳球TOP10 / NO.7 / 第3球 / RANK 5 / 3/10 / BEST 9。
# 这类 token 一旦被整体 remove（inpaint 抹掉），成片就丢失「第几个球」说明，
# 因此必须「保留 + 翻译」，绝不能 blanket-remove。
# 注意：只做机械正则匹配（数字 / 字母模式），不解释视频内容本身。
RANKING_TOKEN_RE = re.compile(
    r"(TOP\s*\d+|NO\.?\s*\d+|RANK\s*\d+|BEST\s*\d+|NO\s*\d+"
    r"|第\s*\d+\s*(?:球|名|位|集|期|强)"
    r"|\d+\s*/\s*\d+)",
    re.IGNORECASE,
)


def extract_ranking_tokens(text: str) -> List[str]:
    """返回文本里所有排名 / 倒计时 token（大写），没有则为空列表。"""
    if not text:
        return []
    return [m.group(0).strip().upper() for m in RANKING_TOKEN_RE.finditer(text)]


def has_ranking_token(text: str) -> bool:
    """文本是否含排名 / 倒计时 token。用于规划守卫与 QA 闸门。"""
    return bool(text) and bool(RANKING_TOKEN_RE.search(text))


# ============================================================
# 路径
# ============================================================
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
CONFIGS_DIR = os.path.join(SKILL_ROOT, "configs")
PROFILES_DIR = os.path.join(SKILL_ROOT, "profiles")
TEMPLATES_DIR = os.path.join(SKILL_ROOT, "templates")
PROMPTS_DIR = os.path.join(SKILL_ROOT, "prompts")
MUSIC_DIR = os.path.join(SKILL_ROOT, "music")

# ============================================================
# 字体解析（封面/标题共用，别再各写一份）
# ============================================================
# profile.cover_style.font_chain 存的是**字体名**（保持可移植），这里映射到
# 本机实际安装的文件。两个封面脚本（generate_cover.py / make_cover.py）都必须
# 走这个函数 —— 2026-09-27 之前 generate_cover 用 ImageFont.truetype("Impact")
# 靠 PIL 自己找、make_cover 干脆写死 Arial Black，同一份 profile 出两种字。
SYSTEM_FONTS = {
    "anton": ["~/Library/Fonts/Anton-Regular.ttf", "~/Library/Fonts/Anton.ttf",
              "/Library/Fonts/Anton-Regular.ttf"],
    "impact": ["/System/Library/Fonts/Supplemental/Impact.ttf",
               "/Library/Fonts/Impact.ttf"],
    "arial black": ["/System/Library/Fonts/Supplemental/Arial Black.ttf",
                    "/Library/Fonts/Arial Black.ttf"],
    "helvetica bold": ["/System/Library/Fonts/Supplemental/HelveticaNeue.ttc",
                       "/Library/Fonts/Helvetica Bold.ttf"],
    "arial bold": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                   "/Library/Fonts/Arial Bold.ttf"],
}
DEFAULT_FONT = "/System/Library/Fonts/Supplemental/Arial Black.ttf"


def resolve_font_chain(chain, fallback: str = DEFAULT_FONT) -> str:
    """First installed face in a font_chain (accepts names or absolute paths)."""
    for name in chain or []:
        name = str(name).strip()
        if not name or name.lower() in ("sans-serif", "sans", "serif"):
            continue
        if os.path.exists(os.path.expanduser(name)):
            return os.path.expanduser(name)
        for p in SYSTEM_FONTS.get(name.lower(), []):
            p = os.path.expanduser(p)
            if os.path.exists(p):
                return p
    return fallback

# ============================================================
# 日志
# ============================================================
def get_logger(name: str = "tiktok-localizer") -> logging.Logger:
    """获取统一格式的 logger"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


log = get_logger()

# ============================================================
# JSON 工具
# ============================================================
def load_json(path: str, default: Any = None) -> Any:
    """加载 JSON 文件，不存在返回 default"""
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.warning(f"Failed to load JSON {path}: {e}")
    return default


def save_json(path: str, obj: Any) -> None:
    """保存 JSON 文件（自动创建目录，ensure_ascii=False，indent=2）"""
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def make_result(
    status: str = "ok",
    confidence: float = 1.0,
    warnings: Optional[List[str]] = None,
    errors: Optional[List[str]] = None,
    **kwargs,
) -> Dict[str, Any]:
    """构造统一的脚本结果 JSON 结构"""
    return {
        "status": status,
        "confidence": round(float(confidence), 4),
        "warnings": warnings or [],
        "errors": errors or [],
        **kwargs,
    }

# ============================================================
# 子进程
# ============================================================
def run(
    cmd: List[str],
    check: bool = True,
    capture: bool = True,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess:
    """运行子进程命令，失败时抛出带 stderr 摘要的异常"""
    log.debug(f"Running: {' '.join(map(str, cmd))}")
    try:
        r = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(map(str, cmd))}") from e
    except FileNotFoundError as e:
        raise RuntimeError(f"Command not found: {cmd[0]}. Is it installed?") from e

    if check and r.returncode != 0:
        stderr_tail = (r.stderr or "")[-2000:]
        raise RuntimeError(
            f"Command failed (exit {r.returncode}): {' '.join(map(str, cmd))}\n"
            f"stderr tail:\n{stderr_tail}"
        )
    return r


def run_cmd_simple(cmd: List[str]) -> Tuple[int, str, str]:
    """简单运行命令，返回 (returncode, stdout, stderr)，不抛异常"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, r.stdout or "", r.stderr or ""
    except Exception as e:
        return -1, "", str(e)

# ============================================================
# 工具检查
# ============================================================
def require_tools(*tools: str) -> None:
    """检查必需的 CLI 工具是否存在，缺失则退出码 2"""
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        log.error(
            f"Missing required deterministic tools: {missing}. "
            f"Install them first (see README.md)."
        )
        sys.exit(EXIT_FATAL)


def check_python_package(pkg_name: str) -> bool:
    """检查 Python 包是否可用"""
    try:
        __import__(pkg_name)
        return True
    except ImportError:
        return False


def require_python_package(pkg_name: str, install_hint: str = "") -> None:
    """检查必需的 Python 包，缺失则退出码 2"""
    if not check_python_package(pkg_name):
        log.error(
            f"Missing required Python package: {pkg_name}. "
            f"{install_hint or f'Install with: pip install {pkg_name}'}"
        )
        sys.exit(EXIT_FATAL)

# ============================================================
# 配置加载
# ============================================================
def load_yaml(path: str) -> Dict[str, Any]:
    """加载 YAML 文件"""
    try:
        import yaml
    except ImportError:
        log.error("PyYAML not installed. Install with: pip install pyyaml")
        sys.exit(EXIT_FATAL)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(work_dir: Optional[str] = None) -> Dict[str, Any]:
    """加载全局默认配置 configs/default.yaml"""
    cfg_path = os.path.join(CONFIGS_DIR, "default.yaml")
    if os.path.exists(cfg_path):
        return load_yaml(cfg_path)
    log.warning(f"Config not found: {cfg_path}, using empty config")
    return {}


def load_providers_config() -> Dict[str, Any]:
    """加载 providers 配置"""
    cfg_path = os.path.join(CONFIGS_DIR, "providers.yaml")
    if os.path.exists(cfg_path):
        return load_yaml(cfg_path)
    return {}


def load_profile(profile_name: str) -> Dict[str, Any]:
    """加载指定 profile，不存在则回退到 generic"""
    profile_path = os.path.join(PROFILES_DIR, f"{profile_name}.yaml")
    if not os.path.exists(profile_path):
        log.warning(f"Profile '{profile_name}' not found at {profile_path}, falling back to generic")
        profile_path = os.path.join(PROFILES_DIR, "generic.yaml")
    if os.path.exists(profile_path):
        return load_yaml(profile_path)
    log.warning(f"Generic profile not found, using empty profile")
    return {}

# ============================================================
# 路径工具
# ============================================================
def ensure_dir(path: str) -> str:
    """确保目录存在，返回路径"""
    os.makedirs(path, exist_ok=True)
    return path


def work_path(work_dir: str, filename: str) -> str:
    """构造 work 目录下的文件路径"""
    return os.path.join(work_dir, filename)


def output_path(output_dir: str, filename: str) -> str:
    """构造 output 目录下的文件路径"""
    return os.path.join(output_dir, filename)


def file_exists(path: str) -> bool:
    """检查文件是否存在且非空"""
    return os.path.exists(path) and os.path.getsize(path) > 0


def safe_filename(text: str) -> str:
    """将文本转换为安全的文件名"""
    import re
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[-\s]+', '_', text)
    return text.strip('_')[:100]

# ============================================================
# 时间工具
# ============================================================
def sec_to_srt_time(seconds: float) -> str:
    """将秒数转换为 SRT 时间格式 HH:MM:SS,mmm"""
    if seconds < 0:
        seconds = 0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:
        secs += 1
        millis = 0
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def srt_time_to_sec(srt_time: str) -> float:
    """将 SRT 时间格式转换为秒数"""
    try:
        h, m, s = srt_time.replace(',', '.').split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return 0.0

# ============================================================
# 降级 / 错误处理
# ============================================================
def die_degraded(msg: str, result_path: Optional[str] = None, result: Optional[Dict] = None) -> None:
    """以可降级失败退出（码 1），可选保存结果 JSON"""
    log.warning(f"[DEGRADED] {msg}")
    if result_path and result is not None:
        result["status"] = "degraded"
        if msg not in result.get("warnings", []):
            result.setdefault("warnings", []).append(msg)
        save_json(result_path, result)
    sys.exit(EXIT_DEGRADED)


def die_fatal(msg: str) -> None:
    """以致命失败退出（码 2）"""
    log.error(f"[FATAL] {msg}")
    sys.exit(EXIT_FATAL)


def add_degradation(result: Dict[str, Any], capability: str, used: str, reason: str) -> None:
    """向结果中添加降级记录"""
    result.setdefault("degradations", []).append({
        "capability": capability,
        "used": used,
        "reason": reason,
    })
    result["degraded"] = True

# ============================================================
# BBox 工具
# ============================================================
def bbox_iou(b1: Dict[str, float], b2: Dict[str, float]) -> float:
    """计算两个 bbox 的 IoU（交并比）"""
    x1 = max(b1["x"], b2["x"])
    y1 = max(b1["y"], b2["y"])
    x2 = min(b1["x"] + b1["w"], b2["x"] + b2["w"])
    y2 = min(b1["y"] + b1["h"], b2["y"] + b2["h"])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area1 = b1["w"] * b1["h"]
    area2 = b2["w"] * b2["h"]
    union = area1 + area2 - inter
    return inter / union if union > 0 else 0.0


def bbox_center(b: Dict[str, float]) -> Tuple[float, float]:
    """获取 bbox 中心点"""
    return (b["x"] + b["w"] / 2, b["y"] + b["h"] / 2)


def bbox_position_zone(b: Dict[str, float], frame_w: int, frame_h: int) -> str:
    """判断 bbox 在画面中的九宫格位置"""
    cx, cy = bbox_center(b)
    col = "left" if cx < frame_w / 3 else ("right" if cx > 2 * frame_w / 3 else "center")
    row = "top" if cy < frame_h / 3 else ("bottom" if cy > 2 * frame_h / 3 else "center")
    return f"{row}_{col}"

# ============================================================
# 文本相似度（词汇重叠，非语义）
# ============================================================
def char_bigram_jaccard(text1: str, text2: str) -> float:
    """中文 char-bigram Jaccard 相似度（词汇重叠，非语义）"""
    def bigrams(s):
        s = ''.join(c for c in s if c.strip())
        return set(s[i:i+2] for i in range(len(s)-1)) if len(s) >= 2 else set(s)
    b1, b2 = bigrams(text1), bigrams(text2)
    if not b1 or not b2:
        return 0.0
    return len(b1 & b2) / len(b1 | b2)


def word_jaccard(text1: str, text2: str) -> float:
    """英文 word Jaccard 相似度（词汇重叠，非语义）"""
    import re
    def words(s):
        return set(re.findall(r'[a-zA-Z]+', s.lower()))
    w1, w2 = words(text1), words(text2)
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / len(w1 | w2)


def text_overlap(text1: str, text2: str) -> float:
    """自动判断语言并计算词汇重叠度（非语义）"""
    # 简单判断：含中文字符用 char-bigram，否则用 word
    def has_chinese(s):
        return any('\u4e00' <= c <= '\u9fff' for c in s)
    if has_chinese(text1) or has_chinese(text2):
        return char_bigram_jaccard(text1, text2)
    return word_jaccard(text1, text2)


# ============================================================
# 常驻叠字窗口归一化（唯一真源，2026-09-28）
# ============================================================
# 背景（2026-09-27 事故）：`title` / `slogan` / `watermark` 这类**常驻**叠字覆盖整片，
# 但 OCR 往往很晚报出它的首次检出（标题头几帧虚化/淡入，OCR 抓不到帧 0），
# 于是 plan 里写成了 `start_time = 1.0`。而渲染与 mask 都会把窗口向前扩 `PAD_HEAD`
# （默认 0.5s），`[0, 0.5s)` 这十几帧**永远不会被清理** —— 05 的顶部中文标题就这样
# 原样留在了成片开头，QA 按 1FPS 抽帧差点抽不到。
#
# 修法：常驻类叠字若「起点落在片头 HEAD_SNAP_SEC 内」**且**「时长 ≥ PERSISTENT_MIN_SEC」，
# 就把起点强制归零。加时长下限是为了不动真正的短下三分之一标题
# （t=2s 出现、只显示 3s 的那种），避免把 inpaint 窗口扩到本来没字的画面上。
#
# 三个调用方共用本函数（改规则只改这里）：
#   1. `prepare_plan.py`  —— 起草 plan 时就归一，让 plan 文件本身正确（**主修复**）
#   2. `generate_masks.py` —— mask 侧兜底，决定 inpaint 的真实时间窗
#   3. `render_overlays.py` —— 绘制侧兜底
PERSISTENT_CATEGORIES = ("title", "slogan", "watermark")
HEAD_SNAP_SEC = 5.0
PERSISTENT_MIN_SEC = 10.0


def normalize_persistent_starts(plan: Dict[str, Any],
                                head_snap: float = HEAD_SNAP_SEC,
                                persistent_min_sec: float = PERSISTENT_MIN_SEC) -> List[Dict[str, Any]]:
    """把「覆盖片头且时长足够」的常驻叠字起点归零（原地修改 plan）。

    返回被改动的条目列表，供调用方打日志/写结果。
    """
    changed: List[Dict[str, Any]] = []
    for it in plan.get("items", []) or []:
        if it.get("category") not in PERSISTENT_CATEGORIES:
            continue
        try:
            s = float(it.get("start_time", 0) or 0)
            e = float(it.get("end_time", 0) or 0)
        except (TypeError, ValueError):
            continue
        if 0.0 < s <= head_snap and (e - s) >= persistent_min_sec:
            changed.append({
                "item_id": it.get("item_id"),
                "category": it.get("category"),
                "old_start_time": s,
            })
            it["start_time"] = 0.0
    return changed


def log_persistent_snaps(changed: List[Dict[str, Any]], where: str = "") -> None:
    """统一打印归一化告警（三个调用方共用一个文案）。"""
    for c in changed:
        log.warning(
            "persistent %s %s: start_time %.2fs -> 0.00s%s (head-covering overlay; "
            "otherwise the opening frames keep the original text - fix the plan to "
            "silence this)",
            c.get("category"), c.get("item_id") or "-", c.get("old_start_time") or 0.0,
            f" [{where}]" if where else "")


# ============================================================
# BGM 曲目名归一化（唯一真源，2026-09-28）
# ============================================================
# 为什么必须归一：`music/sports/` 下同一首歌既有母版也有变速变体
# （`Wallpaper.mp3` / `Wallpaper_v2.mp3`、`Crusade.mp3` / `Crusade_variant.mp3`）。
# 变体是为**破坏音频指纹**做的 `atempo` 变形，但**观众听来还是同一首歌** ——
# 所以在两件事上必须归到同一个 key：
#   1. **防重复**（§12.4）：用了 `Crusade` 又用 `Crusade_variant` = 观众眼里重复了。
#      曲库"有多少首可换"要按**不同歌曲数**算，不是文件数（34 个文件 = 24 首歌）。
#   2. **署名**（`generate_post.py`）：变体与母版是同一份许可、同一个作者。
# 两个调用方共用本函数（改规则只改这里）。
_SEPARATOR_RE = re.compile(r"[\s_\-]+")
_VARIANT_TAIL_RE = re.compile(r"(?:v\d+|variant|remix)+$", re.IGNORECASE)


def normalize_track_key(name: str) -> str:
    """把文件名或曲名归一成可比较的 key（母版与变体同 key）。

    `Wallpaper.mp3` / `Wallpaper_v2.mp3` / `Five Armies_variant.mp3` → `wallpaper` / `fivearmies`
    """
    stem = os.path.splitext(os.path.basename(str(name or "")))[0]
    stem = _SEPARATOR_RE.sub("", stem).lower()
    return _VARIANT_TAIL_RE.sub("", stem)


def is_variant_track(name: str) -> bool:
    """判断是不是 `*_variant` / `*_v2` 这类指纹变形变体。"""
    stem = os.path.splitext(os.path.basename(str(name or "")))[0]
    return bool(_VARIANT_TAIL_RE.search(_SEPARATOR_RE.sub("", stem).lower()))
