# Windows 现场修复版 · 2026-11-07

此发布以 v0.5.6 为基线，包含本机现有 Windows BLE、原子文件保存、停止与标定恢复修复。标签为 `windows-v0.5.6-20261107`；程序内部版本保留 0.5.6。它是独立现场构建，不替换仓库的 v0.5.8 主线。

## 下载使用

Windows 10/11 x64：解压完整 ZIP，双击 `启动覆膜机.cmd` 或 `SensUsBackend.exe`。保留 `_internal`、`workstation`、`tools` 目录。包内包含运行依赖、使用说明、源码快照和许可证，不需要安装 Python 或编译工具链。

新电脑须选择实际串口与板卡，按本机几何完成四点标定后再运行实物。包内没有现场标定、设备绑定、测量数据或自动实验任务。正常启动保留现有 RS485 只读自动探测功能。

## 验证

- Windows 上用 Python 3.12.10 和 `packaging/portable-windows.lock` 原生构建 EXE。
- 215 项有针对性的自动化、恢复、Windows 锁、幂等保存、蓝牙与便携打包测试通过。
- 冻结程序验证了采集/分析命令入口、NumPy/SciPy/BLE 运行依赖、界面和 CAD 资源、自定义重复访问 `[3,1,3]` 与三轮 `[1,3]` 模拟路径、原始样本及 manifest 校验，以及正常退出。
- 验收使用独立状态、工作区和随机端口，并设置 `SENSUS_MOTION_AUTOCONNECT=0`，确认不打开运动串口；模拟样本均明确标为 synthetic。
- 本次打包不移动机构、不替代新设备的标定和实物验收。

## 重建

在 Windows 上检出此标签，按 `windows/build_portable.ps1` 使用 Python 3.12.10 构建。按脚本准备具有对应源码和许可证的固定 OpenOCD/WinUSB 工具。产物位于 `artifacts/releases/0.5.6/`。

用 `packaging/smoke_portable.py --backend <EXE> --resources <workstation目录> --expected-version 0.5.6 --report <JSON>` 运行隔离验收。SHA256SUMS.txt 用于核对发布文件完整性。
