@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo 未检测到 python。请先安装 Python 3：https://www.python.org/downloads/
    echo 安装时请勾选 "Add Python to PATH"，安装后重新双击本文件即可。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo 首次运行，正在创建虚拟环境...
    python -m venv .venv
)

echo 正在检查/安装依赖...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt

echo 正在启动直线模组上位机（默认电机地址 ID=3）...
".venv\Scripts\python.exe" main.py
if %errorlevel% neq 0 (
    pause
)
