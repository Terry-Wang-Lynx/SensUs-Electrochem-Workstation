#!/bin/bash
# macOS 一键启动脚本（首次运行会自动创建虚拟环境并安装依赖）
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未检测到 python3。请先安装 Python 3：https://www.python.org/downloads/"
  echo "安装后重新双击本文件即可。"
  read -r -p "按回车键退出..."
  exit 1
fi

# 独立虚拟环境，不影响系统 Python
if [ ! -d ".venv" ]; then
  echo "首次运行，正在创建虚拟环境..."
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "正在检查/安装依赖..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

echo "启动直线模组上位机..."
python main.py
