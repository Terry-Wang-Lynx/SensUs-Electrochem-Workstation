@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo 需要 python，请先安装：https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist ".venv-build\Scripts\python.exe" (
    python -m venv .venv-build
)

".venv-build\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv-build\Scripts\python.exe" -m pip install --quiet -r requirements.txt pyinstaller

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
".venv-build\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --windowed --name "LinearStage" main.py

echo.
echo 完成：dist\LinearStage.exe（单个可执行文件，可直接分发运行）
pause
