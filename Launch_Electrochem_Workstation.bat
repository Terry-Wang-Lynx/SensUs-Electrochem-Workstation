@echo off
chcp 65001 >nul 2>&1
setlocal

set "ROOT=%~dp0"
cd /d "%ROOT%"
set "SENSUS_PROJECT_DIR=%CD%"
set "PYTHON=%CD%\.venv\Scripts\python.exe"
set "PYTHONW=%CD%\.venv\Scripts\pythonw.exe"

REM --- 1. Create venv if missing ---
if not exist "%PYTHON%" (
    echo Creating Python environment...
    python -m venv "%CD%\.venv"
    if errorlevel 1 (
        echo Failed to create venv.
        pause
        exit /b 1
    )
)

REM --- 2. Install / reinstall if needed ---
set "NEED_INSTALL=0"
if not exist "%CD%\.venv-installed" set "NEED_INSTALL=1"
if %NEED_INSTALL%==0 (
    for %%F in ("%CD%\pyproject.toml") do (
        for %%G in ("%CD%\.venv-installed") do (
            if "%%~tF" gtr "%%~tG" set "NEED_INSTALL=1"
        )
    )
)
if %NEED_INSTALL%==1 (
    echo Installing SensUs workstation...
    "%PYTHON%" -m pip install -e "%CD%" pywebview
    if errorlevel 1 (
        echo Installation failed.
        pause
        exit /b 1
    )
    type nul > "%CD%\.venv-installed"
)

REM --- 3. Launch native app ---
start "" "%PYTHONW%" "%CD%\windows\sensus_app.pyw"
