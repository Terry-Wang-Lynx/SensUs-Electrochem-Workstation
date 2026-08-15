#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
cd "$ROOT"

pause_on_error() {
  local code=$?
  echo
  echo "安装失败，请把这个窗口最后 30 行发给维护者。"
  read -k 1 "?按任意键关闭..."
  exit "$code"
}
trap pause_on_error ZERR

echo "SensUs 工作站 macOS 首次安装"
echo "项目目录: $ROOT"
echo

if ! command -v brew >/dev/null 2>&1; then
  echo "未找到 Homebrew，正在安装（macOS 可能会要求输入密码）..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

if [[ -x /opt/homebrew/bin/brew ]]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
elif [[ -x /usr/local/bin/brew ]]; then
  eval "$(/usr/local/bin/brew shellenv)"
fi

echo "[1/4] 安装 Python 和 OpenOCD..."
brew install python@3.12 open-ocd

PYTHON_PREFIX="$(brew --prefix python@3.12)"
PYTHON=""
for candidate in \
  "$PYTHON_PREFIX/bin/python3.12" \
  "$PYTHON_PREFIX/libexec/bin/python3"; do
  if [[ -x "$candidate" ]]; then
    PYTHON="$candidate"
    break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "找不到 Homebrew Python 3.12，请重新运行本脚本。" >&2
  false
fi

echo "[2/4] 建立独立 Python 环境..."
if [[ -d .venv && ! -x .venv/bin/python3 ]]; then
  mv .venv ".venv.invalid.$(date +%Y%m%d-%H%M%S)"
fi
if [[ ! -x .venv/bin/python3 ]]; then
  "$PYTHON" -m venv .venv
fi
.venv/bin/python3 -m pip install --upgrade pip
.venv/bin/python3 -m pip install -e ".[dev,analysis]"
touch .venv-installed

echo "[3/4] 验证软件..."
.venv/bin/python3 -m pytest software/host/tests -q

echo "[4/4] 检查固件和硬件通道..."
.venv/bin/python3 macos/check_environment.py

# 该脚本经右键打开后，顺便解除同一交付文件夹内 App 的下载隔离标记。
/usr/bin/xattr -dr com.apple.quarantine "$ROOT" 2>/dev/null || true
chmod +x "$ROOT"/*.command "$ROOT"/macos/*.sh 2>/dev/null || true

echo
echo "基础环境已安装。以后双击“02-启动工作站.command”。"
echo "要修改任意固件参数，再双击“03-安装固件工具链.command”。"
if [[ -d "$ROOT/SensUs 电化学工作站.app" ]]; then
  echo "正在打开工作站..."
  /usr/bin/open "$ROOT/SensUs 电化学工作站.app"
fi
echo
read -k 1 "?按任意键关闭..."
