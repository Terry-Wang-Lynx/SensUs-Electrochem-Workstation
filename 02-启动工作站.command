#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
cd "$ROOT"

if [[ ! -x .venv/bin/python3 ]]; then
  echo "尚未安装。请先双击“01-首次安装.command”。"
  read -k 1 "?按任意键关闭..."
  exit 1
fi

if [[ -x /opt/homebrew/bin/brew ]]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
elif [[ -x /usr/local/bin/brew ]]; then
  eval "$(/usr/local/bin/brew shellenv)"
fi

export SENSUS_PROJECT_DIR="$ROOT"
if [[ -z "${SENSUS_NCS_DIR:-}" ]]; then
  if [[ -f "$HOME/sensus-toolchains/ncs/zephyr/zephyr-env.sh" ]]; then
    export SENSUS_NCS_DIR="$HOME/sensus-toolchains/ncs"
  elif [[ -f "$HOME/ncs/zephyr/zephyr-env.sh" ]]; then
    export SENSUS_NCS_DIR="$HOME/ncs"
  else
    export SENSUS_NCS_DIR="$HOME/sensus-toolchains/ncs"
  fi
fi
if [[ -z "${SENSUS_ZEPHYR_SDK_DIR:-}" ]]; then
  if [[ -x "$HOME/sensus-toolchains/zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc" ]]; then
    export SENSUS_ZEPHYR_SDK_DIR="$HOME/sensus-toolchains/zephyr-sdk-1.0.1"
  elif [[ -x "$HOME/zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc" ]]; then
    export SENSUS_ZEPHYR_SDK_DIR="$HOME/zephyr-sdk-1.0.1"
  else
    export SENSUS_ZEPHYR_SDK_DIR="$HOME/sensus-toolchains/zephyr-sdk-1.0.1"
  fi
fi
export SENSUS_NCS_VENV_ACTIVATE="${SENSUS_NCS_VENV_ACTIVATE:-$SENSUS_NCS_DIR/.venv/bin/activate}"

echo "正在启动 SensUs 工作站..."
if [[ -d "$ROOT/SensUs 电化学工作站.app" ]]; then
  /usr/bin/open "$ROOT/SensUs 电化学工作站.app"
  exit 0
fi
exec .venv/bin/python3 -m pa_host.gui_server --open-browser
