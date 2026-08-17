# SensUs 电化学工作站 — Windows 说明

## 快速开始

### 便携包（推荐）

从 GitHub Release 下载 `SensUs-Workstation-Windows-x64-<version>.zip`，先完整解压，
再双击解压目录中的 `SensUsBackend.exe`。便携包已经包含 Python、工作站依赖、
V4/V5.1 随包固件、`smpmgr` 和带 `libjaylink` 的 OpenOCD，不需要安装 Python、
OpenOCD 或 nRF Connect SDK。Windows 10/11 x64 的 USB CDC 和 J-Link 硬件通道均可
直接使用。某些首次接入的 J-Link 若缺少调试接口驱动，设备列表会显示“准备 J-Link”；
点击后确认一次 Windows 管理员提示即可，所需工具已经在包内，不会联网安装其他软件。
若本机没有 WebView2 Runtime，启动器会自动改用系统默认浏览器打开界面。

### 源码目录启动

以下方式只适用于源码开发目录：

#### 方式一：双击启动（推荐）

双击仓库根目录下的 **`Launch_Electrochem_Workstation.bat`**。

首次运行会自动：
1. 创建 Python 虚拟环境 (`.venv`)
2. 安装项目依赖
3. 启动本地 GUI 服务器并打开浏览器

之后每次双击都会直接启动。

也可以双击 **`Launch_Electrochem_Workstation.ps1`**（PowerShell 版本），
右键选择"使用 PowerShell 运行"。

启动后在浏览器中访问：`http://127.0.0.1:8765/`

#### 方式二：Python 启动器

```cmd
python windows/run_app.py
```

#### 方式三：命令行

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

构建机需要 Python 3.12、Git 和 Visual Studio 2022 C++ Build Tools。打包脚本会在
`artifacts/build-env/` 创建隔离环境，从固定提交和校验过的依赖构建 libwdi helper，
再用 PyInstaller 生成 onedir 后端，并打包 WebView2 原生窗口、OpenOCD/libjaylink、
WinUSB 准备工具和 V4/V5.1 稳定固件资源。

输出：`artifacts\releases\<version>\SensUs-Workstation-Windows-x64-<version>.zip`

便携版内置 V4 RTT/J-Link 与 V5.1 USB CDC 的通用运行时固件。点击“应用条件”时，
软件会自动准备随包镜像；开始测量前再把当前 I-T/CV 条件整组下发并核验，所以修改
电位、时序、量程、CV 范围/速度/圈数和 EIS 档位都不需要安装 NCS、Zephyr、west 或
编译器。源码目录仍可安装这些工具来开发和编译全新固件协议，但不是使用便携版的前置条件。

## 硬件控制

硬件控制模式（编译/烧录固件、RTT 采集）需要：

### J-Link 调试探头
- 便携包默认使用随包 OpenOCD/libjaylink，不需要安装 SEGGER J-Link 软件。
- 若 Windows 尚未给受支持的 J-Link 调试接口绑定 WinUSB，界面只在确认设备管理器
  对应接口异常后显示“准备 J-Link”；确认一次 UAC 后会自动重新检测目标板。
- 多探头时可在界面中手动选择设备；也可以设置 `SENSUS_JLINK_SERIAL` 指定序列号。
- 源码目录仍可优先使用 `SENSUS_JLINK_EXE` 指向的 JLink.exe。

### OpenOCD（源码目录）
- 便携包已自带 OpenOCD 和 scripts，无需配置环境变量。
- 源码目录可设置 `SENSUS_OPENOCD_EXE` 指向 openocd.exe，或设置
  `SENSUS_OPENOCD_SCRIPTS` 指向 scripts 目录。

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
| 通用固件烧录与自定义条件 | ✅ | ✅ (便携版零工具链) |
| 新增固件协议的现场编译 | ✅ (需 NCS) | ✅ (需 NCS) |
| 硬件采集 | ✅ (USB/J-Link) | ✅ (USB/J-Link) |
| CV 分析 | ✅ | ✅ |
| I-T 分析 | ✅ | ✅ |
| 标定/预测 | ✅ | ✅ |
| 自动测量 | ✅ | ✅ |

> **Windows 用户**：源码启动继续使用浏览器；便携包优先使用 WebView2 原生窗口，
> 缺少 WebView2 Runtime 时自动退回系统浏览器。
> I-T/CV 采集、标定、分析、预测和自动测量共用同一后端。

## 故障排查

### "python 不是内部或外部命令"
→ 安装 Python 3.10+ 并勾选"Add Python to PATH"。

### "无法启动采集进程"
→ 检查探头和目标板供电。若设备列表显示“准备 J-Link”，点击并确认一次 Windows
管理员提示；便携包不需要另外下载 J-Link 软件。

### "找不到 JLinkExe / JLink.exe"
→ 便携包会自动回退到随包 OpenOCD；只有源码目录需要设置
  `SENSUS_JLINK_EXE` 或 `SENSUS_OPENOCD_EXE`。

### "pip install 失败"
→ 尝试以管理员身份运行，或检查网络连接。
