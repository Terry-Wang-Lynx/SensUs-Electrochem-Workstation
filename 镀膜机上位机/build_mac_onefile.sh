#!/bin/bash
# 在 Mac 上打包成【单文件】可执行程序（无需安装 Python，双击即运行）
# 产物：dist/LinearStage（单个 Mach-O 可执行文件）
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
pyinstaller --noconfirm --clean --onefile --windowed --name "LinearStage" main.py

echo ""
echo "完成：dist/LinearStage （单个可执行文件，可直接发给别人）"
echo "首次打开若提示「无法验证开发者」：右键 → 打开；或终端执行："
echo "  xattr -cr dist/LinearStage"
echo ""
echo "提示：若运行时报缺少 Qt 插件（platform plugin 之类），在下面命令加上 --collect-all PySide6 重新打包："
echo "  pyinstaller --noconfirm --clean --onefile --windowed --collect-all PySide6 --name LinearStage main.py"
