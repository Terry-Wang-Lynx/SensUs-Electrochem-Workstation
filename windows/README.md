# SensUs 电化学工作站 — Windows 说明

## 快速开始

### 方式一：双击启动（推荐）

双击仓库根目录下的 **`Launch_Electrochem_Workstation.bat`**。

首次运行会自动：
1. 创建 Python 虚拟环境 (`.venv`)
2. 安装项目依赖
3. 启动本地 GUI 服务器并打开浏览器

之后每次双击都会直接启动。

也可以双击 **`Launch_Electrochem_Workstation.ps1`**（PowerShell 版本），
右键选择"使用 PowerShell 运行"。

启动后在浏览器中访问：`http://127.0.0.1:8765/`

### 方式二：Python 启动器

```cmd
python windows/run_app.py
```

### 方式三：命令行

```cmd
python -m venv .venv
.venv\Scripts\python -m pip install -e .
set SENSUS_PROJECT_DIR=%CD%
.venv\Scripts\python -m pa_host.gui_server --open-browser
```

## 一键启动

双击 `Launch_Electrochem_Workstation.bat`。

浏览器会自动打开 `http://127.0.0.1:8765/`。

## 打包为独立 EXE

```cmd
windows\build_win.bat
```

需要预先安装 Python 3.10+。打包脚本会在 `artifacts/build-env/` 创建隔离环境，
用 PyInstaller 生成 onedir 后端，并打包 WebView2 原生窗口、V4/V5.1 稳定固件资源。

输出：`artifacts\releases\<version>\SensUs-Workstation-Windows-x64-<version>.zip`

> **注意**：便携版可以采集并使用随包稳定固件；如果要编译自定义条件，仍需从完整
> 源码仓库运行，并安装：
> - [nRF Connect SDK](https://www.nordicsemi.com/Products/Development-software/nRF-Connect-SDK) (Zephyr RTOS + west)
> - [SEGGER J-Link](https://www.segger.com/downloads/jlink/) (V8.80 推荐)
> - [GNU Arm Embedded Toolchain](https://developer.arm.com/tools-and-software/open-source-software/developer-tools/gnu-toolchain)

## 硬件控制

硬件控制模式（编译/烧录固件、RTT 采集）需要：

### J-Link 调试探头
- 推荐 SEGGER J-Link V8.80（V9.x 不兼容克隆探头）
- 设置环境变量 `SENSUS_JLINK_EXE` 指向 JLink.exe 路径
- 设置 `SENSUS_JLINK_SERIAL` 指定探头序列号（多探头时）
- 默认搜索路径:
  - `C:\Program Files\SEGGER\JLink\JLink.exe`
  - `C:\ST\STM32CubeIDE\...\tools\bin\JLink.exe`

### OpenOCD（J-Link 不可用时的回退方案）
- 设置 `SENSUS_OPENOCD_EXE` 指向 openocd.exe
- 设置 `SENSUS_OPENOCD_SCRIPTS` 指向 scripts 目录
- 默认搜索: `C:\Program Files\OpenOCD\`

### nRF Connect SDK（编译固件用）
- 默认路径: `%USERPROFILE%\ncs`
- 使用 Zephyr RTOS 环境编译固件
- 设置 `SENSUS_NCS_VENV_ACTIVATE` 指向 NCS venv 的激活脚本（如 `%USERPROFILE%\ncs\.venv\Scripts\activate.bat`）

## 与 macOS 版本的差异

| 功能 | macOS | Windows |
|------|-------|---------|
| 原生 App 窗口 | ✅ Swift + AppKit | ✅ WebView2（便携版） |
| 悬浮检测窗 | ✅ 画中画模式 | ❌ |
| 窗口置顶 | ✅ 工具栏按钮 | ❌ |
| 浏览器界面 | ✅ | ✅ |
| Python 命令行 | ✅ | ✅ |
| 固件编译 | ✅ | ✅ (需 NCS + Zephyr) |
| 硬件采集 | ✅ | ✅ (需 J-Link) |
| CV 分析 | ✅ | ✅ |
| I-T 分析 | ✅ | ✅ |
| 标定/预测 | ✅ | ✅ |
| 自动测量 | ✅ | ✅ |

> **Windows 用户**：源码启动继续使用浏览器；便携包默认使用 WebView2 原生窗口。
> I-T/CV 采集、标定、分析、预测和自动测量共用同一后端。

## 故障排查

### "python 不是内部或外部命令"
→ 安装 Python 3.10+ 并勾选"Add Python to PATH"。

### "无法启动采集进程"
→ 检查 J-Link 驱动是否正确安装，探头是否连接。

### "找不到 JLinkExe / JLink.exe"
→ 设置环境变量 `SENSUS_JLINK_EXE` 指向 JLink.exe 完整路径。
  例如: `set SENSUS_JLINK_EXE=C:\Program Files\SEGGER\JLink\JLink.exe`

### "pip install 失败"
→ 尝试以管理员身份运行，或检查网络连接。
