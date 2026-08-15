#!/usr/bin/env python3
"""SensUs 电化学工作站 — Windows 原生启动器。

双击此脚本或运行 ``python windows/run_app.py`` 即可:
1. 自动创建/检查项目 venv
2. 安装 Python 包
3. 启动本地 GUI 服务器
4. 在默认浏览器中打开工作站界面

关闭此窗口即停止服务。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
VENV_DIR = PROJECT_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
STAMP_FILE = PROJECT_DIR / ".venv-installed"
PYPROJECT = PROJECT_DIR / "pyproject.toml"


def setup_venv() -> Path:
    """确保 venv 存在且已安装项目包。"""
    if not VENV_PYTHON.exists():
        print("正在创建 Python 虚拟环境...")
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            check=True, cwd=PROJECT_DIR,
        )
        print("虚拟环境创建完成。")

    need_install = False
    if not STAMP_FILE.exists():
        need_install = True
    elif PYPROJECT.stat().st_mtime > STAMP_FILE.stat().st_mtime:
        print("pyproject.toml 已更新，重新安装...")
        need_install = True

    if need_install:
        print("正在安装 SensUs 工作站...")
        subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "install", "-e", str(PROJECT_DIR)],
            check=True, cwd=PROJECT_DIR,
        )
        STAMP_FILE.touch()
        print("安装完成。")

    return VENV_PYTHON


def main() -> int:
    os.chdir(PROJECT_DIR)
    env = os.environ.copy()
    env["SENSUS_PROJECT_DIR"] = str(PROJECT_DIR)
    host_dir = str(PROJECT_DIR / "software" / "host")
    env["PYTHONPATH"] = host_dir + os.pathsep + env.get("PYTHONPATH", "")

    python = setup_venv()

    print("正在启动 SensUs 电化学工作站...")
    server_proc = subprocess.Popen(
        [str(python), "-m", "pa_host.gui_server", "--host", "127.0.0.1",
         "--port", "8765", "--open-browser"],
        cwd=PROJECT_DIR,
        env=env,
    )
    print(f"服务器 PID: {server_proc.pid}")
    print("工作站运行中: http://127.0.0.1:8765/")
    print()
    print("按 Ctrl+C 或关闭此窗口即可停止服务。")
    print("─" * 50)

    try:
        server_proc.wait()
    except KeyboardInterrupt:
        print("\n正在停止工作站...")
        server_proc.terminate()
        try:
            server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        print("工作站已停止。")

    return server_proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
