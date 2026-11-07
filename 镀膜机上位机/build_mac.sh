#!/bin/bash
# 在 Mac 上把上位机打包成独立 .app（双击可运行，目标机器无需安装 Python）
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 python3，请先安装：https://www.python.org/downloads/"
  exit 1
fi

if [ ! -d ".venv-build" ]; then
  python3 -m venv .venv-build
fi
source .venv-build/bin/activate

python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt pyinstaller

rm -rf build dist
pyinstaller --noconfirm --clean --windowed --name "LinearStage" main.py

echo ""
echo "完成：dist/LinearStage.app"
echo "可把它拖进「应用程序」，或在访达里改名为「直线模组.app」。"
echo "首次打开若提示「无法验证开发者」：右键 .app → 打开；或终端执行："
echo "  xattr -cr \"dist/LinearStage.app\""
