@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_portable.ps1"
exit /b %ERRORLEVEL%
