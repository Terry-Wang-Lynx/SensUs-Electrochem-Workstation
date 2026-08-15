@echo off
REM ============================================================
REM  SensUs 电化学工作站 — Windows 打包脚本
REM
REM  生成独立的 Windows 可执行文件（需要 PyInstaller）
REM  用法: build_win.bat
REM  输出: dist\SensUs-Workstation.exe
REM ============================================================

setlocal enabledelayedexpansion
set "ROOT=%~dp0.."
cd /d "%ROOT%"

echo ============================================================
echo   SensUs 电化学工作站 — Windows 打包
echo ============================================================
echo.

REM --- 检查 Python 环境 ---
where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python。请先安装 Python 3.10+ 并加入 PATH。
    pause
    exit /b 1
)

REM --- 创建/检查 venv ---
set "VENV=%ROOT%\.venv"
set "PYTHON=%VENV%\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo [1/4] 创建虚拟环境...
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
)

REM --- 安装依赖 ---
echo [2/4] 安装项目及打包依赖...
"%PYTHON%" -m pip install -e "%ROOT%" pyinstaller Pillow
if errorlevel 1 (
    echo [错误] 安装失败。
    pause
    exit /b 1
)

REM --- 生成图标 ---
echo [3/4] 生成 Windows 图标...
set "ICON=%ROOT%\windows\SensUs-Workstation.ico"
if not exist "%ICON%" (
    "%PYTHON%" "%ROOT%\windows\create_icon.py" "%ICON%"
    if errorlevel 1 (
        echo [警告] 图标生成失败，继续打包...
        set "ICON_ARG="
    ) else (
        set "ICON_ARG=--icon=%ICON%"
    )
) else (
    set "ICON_ARG=--icon=%ICON%"
)

REM --- PyInstaller 打包 ---
echo [4/4] 用 PyInstaller 打包...
"%PYTHON%" -m PyInstaller ^
    --onefile ^
    --console ^
    --name "SensUs-Workstation" ^
    !ICON_ARG! ^
    --add-data "%ROOT%\software\host\pa_host\gui;pa_host\gui" ^
    --add-data "%ROOT%\software\host\pa_host\*.py;pa_host" ^
    --hidden-import pa_host ^
    --hidden-import pa_host.gui_server ^
    --hidden-import pa_host.it ^
    --hidden-import pa_host.collect ^
    --hidden-import pa_host.cv ^
    --hidden-import pa_host.record ^
    --hidden-import pa_host.it_tool ^
    --hidden-import pa_host.analyze ^
    --hidden-import pa_host.synth ^
    --hidden-import pa_host.__main__ ^
    --hidden-import pa_host.__init__ ^
    --collect-all pa_host ^
    "%ROOT%\windows\run_app.py"

if errorlevel 1 (
    echo [错误] 打包失败。
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   打包完成！
echo   输出: %ROOT%\dist\SensUs-Workstation.exe
echo ============================================================
echo.
echo 提示：可直接运行 EXE，或将 dist\SensUs-Workstation.exe
echo       复制到任意位置运行。
echo.
pause
