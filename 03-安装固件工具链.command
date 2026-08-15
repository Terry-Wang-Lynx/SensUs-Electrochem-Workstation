#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
TOOL_ROOT="${SENSUS_TOOLCHAIN_ROOT:-$HOME/sensus-toolchains}"
NCS_DIR="$TOOL_ROOT/ncs"
SDK_DIR="$TOOL_ROOT/zephyr-sdk-1.0.1"
NCS_REV="v3.4.0"

pause_on_error() {
  local code=$?
  echo
  echo "固件工具链安装失败，请把这个窗口最后 30 行发给维护者。"
  read -k 1 "?按任意键关闭..."
  exit "$code"
}
trap pause_on_error ZERR

cd "$ROOT"
if ! command -v brew >/dev/null 2>&1 || [[ ! -x "$ROOT/.venv/bin/python3" ]]; then
  echo "请先双击“01-首次安装.command”。" >&2
  false
fi
if [[ -x /opt/homebrew/bin/brew ]]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
elif [[ -x /usr/local/bin/brew ]]; then
  eval "$(/usr/local/bin/brew shellenv)"
fi

echo "注意：完整 NCS/Zephyr 工具链需要下载大量文件，建议预留 8 GB 空间。"
echo "安装到: $TOOL_ROOT"
echo

echo "[1/6] 安装系统编译依赖..."
brew install cmake ninja gperf python@3.12 ccache dtc libmagic wget open-ocd
mkdir -p "$TOOL_ROOT"
mkdir -p "$NCS_DIR"

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
  echo "找不到 Homebrew Python 3.12，请先重新运行“01-首次安装.command”。" >&2
  false
fi
if [[ ! -d "$NCS_DIR/.venv" ]]; then
  "$PYTHON" -m venv "$NCS_DIR/.venv"
fi
"$NCS_DIR/.venv/bin/python" -m pip install --upgrade pip west

echo "[2/6] 安装 nRF Connect SDK $NCS_REV..."
if [[ ! -f "$NCS_DIR/.west/config" ]]; then
  mkdir -p "$NCS_DIR"
  cd "$NCS_DIR"
  "$NCS_DIR/.venv/bin/west" init -m https://github.com/nrfconnect/sdk-nrf --mr "$NCS_REV"
fi
cd "$NCS_DIR"
"$NCS_DIR/.venv/bin/west" update --narrow -o=--depth=1

echo "[3/6] 安装 NCS Python 依赖..."
"$NCS_DIR/.venv/bin/python" -m pip install -r zephyr/scripts/requirements.txt
"$NCS_DIR/.venv/bin/python" -m pip install -r nrf/scripts/requirements.txt
"$NCS_DIR/.venv/bin/python" -m pip install -r bootloader/mcuboot/scripts/requirements.txt

echo "[4/6] 安装 Zephyr SDK 1.0.1（仅 ARM 工具链）..."
source "$NCS_DIR/zephyr/zephyr-env.sh"
if [[ ! -x "$SDK_DIR/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc" ]]; then
  "$NCS_DIR/.venv/bin/west" sdk install --version 1.0.1 \
    --install-dir "$SDK_DIR" --gnu-toolchains arm-zephyr-eabi
fi

echo "[5/6] 编译项目固件（不烧录）..."
cd "$ROOT"
export SENSUS_NCS_DIR="$NCS_DIR"
export SENSUS_ZEPHYR_SDK_DIR="$SDK_DIR"
export SENSUS_NCS_VENV_ACTIVATE="$NCS_DIR/.venv/bin/activate"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
export ZEPHYR_SDK_INSTALL_DIR="$SDK_DIR"
source "$NCS_DIR/.venv/bin/activate"
source "$NCS_DIR/zephyr/zephyr-env.sh"
west build -p always -b pa_converter_v40 -d software/firmware/build \
  software/firmware -- -DBOARD_ROOT="$ROOT/software/firmware" \
  -DDTS_ROOT="$ROOT/software/firmware"

echo "[6/6] 最终自检..."
"$ROOT/.venv/bin/python3" "$ROOT/macos/check_environment.py" --require-toolchain

echo
echo "完整固件工具链已安装。现在可以在界面中修改任意条件并编译/烧录。"
read -k 1 "?按任意键关闭..."
