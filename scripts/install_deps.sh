#!/usr/bin/env bash
# install_deps.sh - TikTok Video Localizer 依赖安装脚本
#
# 用法：
#   bash scripts/install_deps.sh           # 安装核心依赖
#   bash scripts/install_deps.sh --full    # 安装核心 + OCR + ASR 全部依赖
#   bash scripts/install_deps.sh --ocr     # 仅安装 OCR 依赖
#   bash scripts/install_deps.sh --asr     # 仅安装 ASR 依赖
#   bash scripts/install_deps.sh --check   # 仅检查依赖状态
#
# 支持：macOS (Homebrew) / Linux (apt/yum)

set -e

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 解析参数
INSTALL_CORE=true
INSTALL_OCR=false
INSTALL_ASR=false
CHECK_ONLY=false

for arg in "$@"; do
    case $arg in
        --full)
            INSTALL_OCR=true
            INSTALL_ASR=true
            ;;
        --ocr)
            INSTALL_OCR=true
            ;;
        --asr)
            INSTALL_ASR=true
            ;;
        --check)
            CHECK_ONLY=true
            ;;
        --help|-h)
            echo "用法: bash scripts/install_deps.sh [--full|--ocr|--asr|--check]"
            echo ""
            echo "选项："
            echo "  --full    安装核心 + OCR + ASR 全部依赖"
            echo "  --ocr     仅安装 OCR 依赖（rapidocr 为默认；paddleocr/tesseract 可选）"
            echo "  --asr     仅安装 ASR 依赖（openai-whisper）"
            echo "  --check   仅检查依赖状态，不安装"
            echo "  (无参数)  安装核心依赖"
            exit 0
            ;;
    esac
done

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 检测操作系统
detect_os() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macos"
    elif [[ -f /etc/os-release ]]; then
        . /etc/os-release
        echo "$ID"
    else
        echo "unknown"
    fi
}

OS=$(detect_os)
info "操作系统: $OS"

# 检查命令是否存在
check_cmd() {
    command -v "$1" >/dev/null 2>&1
}

# 检查 Python 包
check_python_pkg() {
    python3 -c "import $1" 2>/dev/null
}

# ============================================================
# 检查函数
# ============================================================

check_system_tools() {
    info "检查系统工具..."
    local all_ok=true

    if check_cmd ffmpeg; then
        success "ffmpeg: $(ffmpeg -version 2>&1 | head -1 | awk '{print $2, $3}')"
    else
        warn "ffmpeg: 未安装"
        all_ok=false
    fi

    if check_cmd ffprobe; then
        success "ffprobe: 已安装"
    else
        warn "ffprobe: 未安装"
        all_ok=false
    fi

    echo "$all_ok"
}

check_python_deps() {
    info "检查 Python 核心依赖..."

    if check_python_pkg cv2; then
        success "opencv-python: $(python3 -c 'import cv2; print(cv2.__version__)')"
    else
        warn "opencv-python: 未安装"
    fi

    if check_python_pkg PIL; then
        success "pillow: $(python3 -c 'from PIL import Image; print(Image.__version__)')"
    else
        warn "pillow: 未安装"
    fi

    if check_python_pkg numpy; then
        success "numpy: $(python3 -c 'import numpy; print(numpy.__version__)')"
    else
        warn "numpy: 未安装"
    fi

    if check_python_pkg yaml; then
        success "pyyaml: 已安装"
    else
        warn "pyyaml: 未安装"
    fi
}

check_ocr_deps() {
    info "检查 OCR 依赖..."

    if check_cmd tesseract; then
        success "tesseract: $(tesseract --version 2>&1 | head -1)"
        # 检查中文语言包
        if tesseract --list-langs 2>&1 | grep -q "chi_sim"; then
            success "tesseract 中文语言包: 已安装 (chi_sim)"
        else
            warn "tesseract 中文语言包: 未安装 (需要 chi_sim)"
        fi
    else
        warn "tesseract: 未安装"
    fi

    if check_python_pkg rapidocr_onnxruntime; then
        success "rapidocr-onnxruntime: 已安装 (默认 OCR 引擎)"
    else
        warn "rapidocr-onnxruntime: 未安装 (推荐安装：默认中文 OCR 引擎，初始化约 2.6s)"
    fi

    if check_python_pkg paddleocr; then
        success "paddleocr: 已安装"
    else
        warn "paddleocr: 未安装 (可选；与 rapidocr 同源，初始化慢约 5 倍)"
    fi

    if check_python_pkg pytesseract; then
        success "pytesseract: 已安装"
    else
        warn "pytesseract: 未安装"
    fi
}

check_asr_deps() {
    info "检查 ASR 依赖..."

    if check_python_pkg whisper; then
        success "openai-whisper: 已安装"
    else
        warn "openai-whisper: 未安装"
    fi

    if check_cmd ffmpeg; then
        # whisper 需要 ffmpeg
        :
    fi
}

# ============================================================
# 安装函数
# ============================================================

install_system_tools() {
    info "安装系统工具 (ffmpeg)..."

    if [[ "$OS" == "macos" ]]; then
        if check_cmd brew; then
            brew install ffmpeg
            success "ffmpeg 安装完成"
        else
            error "Homebrew 未安装，请先安装 Homebrew: https://brew.sh/"
            exit 1
        fi
    elif [[ "$OS" == "ubuntu" || "$OS" == "debian" ]]; then
        sudo apt-get update
        sudo apt-get install -y ffmpeg
        success "ffmpeg 安装完成"
    elif [[ "$OS" == "centos" || "$OS" == "rhel" || "$OS" == "fedora" ]]; then
        sudo yum install -y ffmpeg || sudo dnf install -y ffmpeg
        success "ffmpeg 安装完成"
    else
        warn "不支持的操作系统: $OS，请手动安装 ffmpeg"
    fi
}

install_python_deps() {
    info "安装 Python 核心依赖..."

    # 使用 opencv-python-headless 而不是 opencv-python（更小，无 GUI 依赖，预编译 wheel 更多）
    pip3 install --upgrade pip
    pip3 install opencv-python-headless pillow numpy pyyaml

    success "Python 核心依赖安装完成"
}

install_ocr_deps() {
    info "安装 OCR 依赖..."

    # 安装 tesseract（系统级）
    if [[ "$OS" == "macos" ]]; then
        if check_cmd brew; then
            brew install tesseract tesseract-lang
            success "tesseract 安装完成（含语言包）"
        else
            error "Homebrew 未安装"
            exit 1
        fi
    elif [[ "$OS" == "ubuntu" || "$OS" == "debian" ]]; then
        sudo apt-get install -y tesseract-ocr tesseract-ocr-chi-sim
        success "tesseract 安装完成（含中文语言包）"
    elif [[ "$OS" == "centos" || "$OS" == "rhel" || "$OS" == "fedora" ]]; then
        sudo yum install -y tesseract tesseract-langpack-chi_sim
        success "tesseract 安装完成"
    else
        warn "不支持的操作系统: $OS，请手动安装 tesseract"
    fi

    # 安装 Python OCR 包
    # rapidocr-onnxruntime 是首选：纯 onnxruntime、无 native 依赖、中文美术字
    # 召回高（实测 4/4），初始化约 2.6s，体积远小于 paddleocr。
    info "安装 RapidOCR（首选 OCR 引擎，基于 onnxruntime）..."
    pip3 install rapidocr-onnxruntime

    # tesseract 仅作显式排障选项，不参与 auto 降级：
    # 它对中文描边美术字召回率≈0，若被静默选中会让「能力缺失」伪装成「没有发现问题」
    # （2026-09-22 事故：C 视频 4 处中文只报 1 条误报 → 漏清理 → 假 PASS）。
    info "安装 pytesseract（仅供显式排障，非默认引擎）..."
    pip3 install pytesseract

    # paddleocr 可选：与 rapidocr 同源 PP-OCRv4，效果相当。沙箱内**可以运行**
    # （2026-09-22 实测通过），但初始化慢约 5 倍、需一并装入 379MB 的 paddlepaddle，按需安装。
    if [ "${INSTALL_PADDLEOCR:-0}" = "1" ]; then
        info "安装 paddleocr（可选，体积较大，可能需要几分钟）..."
        pip3 install "paddleocr==2.7.3" paddlepaddle
    else
        info "跳过 paddleocr（可选）。如需安装：INSTALL_PADDLEOCR=1 bash scripts/install_deps.sh --ocr"
    fi

    success "OCR 依赖安装完成"
}

install_asr_deps() {
    info "安装 ASR 依赖 (faster-whisper)..."

    # faster-whisper 比 openai-whisper 更快，且兼容性更好
    # 注意：Python 3.12+ 上 openai-whisper 可能因 pkg_resources 移除而安装失败
    # 优先使用 faster-whisper

    # 先安装 PyAV 预编译 wheel（faster-whisper 可选依赖，用于音频解码）
    info "安装 PyAV（音频解码）..."
    pip3 install av --only-binary :all: || warn "PyAV 预编译 wheel 不可用，跳过（faster-whisper 仍可工作）"

    # 安装 faster-whisper 核心依赖
    info "安装 faster-whisper 核心依赖..."
    pip3 install ctranslate2 tokenizers huggingface_hub

    # 用 --no-deps 安装 faster-whisper（避免重新构建 av）
    info "安装 faster-whisper..."
    pip3 install faster-whisper --no-deps

    success "ASR 依赖安装完成 (faster-whisper)"
    info "提示：首次使用时会自动下载模型文件（base 模型约 150MB）"
    info "提示：audio_processor.py 会自动检测 faster-whisper 并优先使用"
}

# ============================================================
# 主流程
# ============================================================

echo ""
echo "=========================================="
echo "  TikTok Video Localizer - 依赖安装"
echo "=========================================="
echo ""

# 检查模式
if [[ "$CHECK_ONLY" == true ]]; then
    info "仅检查模式，不安装任何依赖"
    echo ""
    check_system_tools
    echo ""
    check_python_deps
    echo ""
    check_ocr_deps
    echo ""
    check_asr_deps
    echo ""
    success "依赖检查完成"
    exit 0
fi

# 安装系统工具
if [[ "$INSTALL_CORE" == true ]]; then
    if ! check_cmd ffmpeg || ! check_cmd ffprobe; then
        install_system_tools
    else
        success "系统工具已安装，跳过"
    fi
    echo ""
fi

# 安装 Python 核心依赖
if [[ "$INSTALL_CORE" == true ]]; then
    install_python_deps
    echo ""
fi

# 安装 OCR 依赖
if [[ "$INSTALL_OCR" == true ]]; then
    install_ocr_deps
    echo ""
fi

# 安装 ASR 依赖
if [[ "$INSTALL_ASR" == true ]]; then
    install_asr_deps
    echo ""
fi

# 最终检查
echo "=========================================="
info "最终依赖检查:"
echo "------------------------------------------"
check_system_tools > /dev/null && success "系统工具: OK" || warn "系统工具: 部分缺失"
check_python_deps > /dev/null 2>&1
echo "------------------------------------------"
echo ""
success "依赖安装完成！"
echo ""
info "快速开始："
echo "  python3 scripts/run_pipeline.py --input input/source.mp4 --work-dir work --output output --profile nba"
echo ""
