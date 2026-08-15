#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h}"
cd "$ROOT"
code=0
if [[ ! -x .venv/bin/python3 ]]; then
  echo "请先双击“01-首次安装.command”。"
  code=1
else
  .venv/bin/python3 macos/check_environment.py || code=$?
fi
echo
read -k 1 "?按任意键关闭..."
exit "$code"
