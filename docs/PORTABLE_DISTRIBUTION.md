# 上位机分发与前端热更新

## 分发模型

Windows 与 macOS 使用同一套 Python LTS 后端和本地 HTTP API。后端随安装包冻结，
不会从网页执行硬件控制代码；前端仍由 `127.0.0.1` 本地服务提供，因此浏览器同源
限制和现有控制接口校验继续有效。

- macOS：Apple Silicon、macOS 14+，输出自包含 DMG。未配置 Apple Developer ID
  时只能 ad-hoc 签名，从网络下载后需右键“打开”确认一次；正式对外发布
  应使用 Developer ID 签名、notarization 和 stapling。
- Windows：Windows 10/11 x64，输出包含冻结后端、OpenOCD/libjaylink、受限的 WinUSB
  准备工具和固件资源的 ZIP。
  WebView2 Runtime 是可选的；若系统没有它，启动器自动退回系统默认浏览器。
- 源码开发：`make app`、`make run`、现有烧录和测量命令保持不变。
- 便携版数据：配置和缓存放系统用户目录，实验数据默认放 `Documents/SensUs Measurements`。

本机构建：

```bash
make portable-macos
make dmg
```

Windows 原生构建：

```powershell
./windows/build_portable.ps1
```

构建脚本会从 OpenOCD 官方 v0.12.0 Release 下载固定 SHA-256 的 Windows 归档，
并将 `openocd.exe`、运行库和 `interface/jlink.cfg` 等 scripts 放入 ZIP。运行时不
需要目标电脑安装 Python、OpenOCD、nRF Connect SDK 或 SEGGER J-Link 软件。它还会
从固定的 libwdi 1.5.1 提交构建 `wdi-simple.exe`，并把完整对应源码和许可证随包分发。
只有 OpenOCD 报告不可访问、且 Windows 设备管理器确认受支持的 J-Link 调试接口异常
时，界面才允许用户点击“准备 J-Link”并确认一次 UAC；不会改动 CDC 接口。
如果电脑已安装兼容的 SEGGER Commander，会在包内 OpenOCD 无法识别目标时
作为备用通道；没安装时仍使用包内 OpenOCD。

产物只写入 `artifacts/releases/<version>/`。Windows EXE 必须在 Windows 或对应 CI
runner 上生成，不能从 macOS 交叉打包。

## 稳定前端更新

安装包内始终保留一份前端。配置了稳定频道后，后端会在启动时和之后每 30 分钟
检查一次签名 manifest，但只有在没有测量、自动任务或固件操作时才会联网下载。
下载完成只标记为 pending，重启应用后才启用；新前端没有调用 ready 接口时，下次
启动自动回退到前一个成功版本。更新 ZIP 经过 SHA256、Ed25519、API major、必需文件
和路径穿越检查。

两个平台同时热更新的关键是让它们在构建时写入相同的：

- `SENSUS_FRONTEND_MANIFEST_URL`
- `SENSUS_FRONTEND_PUBLIC_KEY`

GitHub Actions 从同名 repository variables 注入。未配置这两项时更新器完全禁用，
软件只使用安装包内前端。

建议使用独立的公开、仅含 `gui/` 静态资源和 release 文件的仓库。私钥只保存在发布
机器或 GitHub Actions secret，不进入任一仓库。生成稳定 ZIP 与 manifest：

```bash
.venv/bin/python -m pip install -e '.[portable]'
PYTHONPATH=software/host .venv/bin/python packaging/sign_frontend.py \
  software/host/pa_host/gui \
  --version 2026.08.1 \
  --zip-url https://github.com/ORG/FRONTEND/releases/download/2026.08.1/frontend-2026.08.1.zip \
  --private-key /secure/path/frontend-ed25519.pem \
  --output /tmp/sensus-frontend-release
```

上传 ZIP 后，把 `stable.json` 放到固定 URL。客户端只接受 `channel=stable`，不会跟随
测试频道。不要把 GitHub Pages 直接嵌入 App，也不要为远程网页开放硬件 API CORS。

## 固件资源边界

便携包包含两套“通用运行时固件”：V4 的 RTT/J-Link 镜像和 V5.1 的 USB CDC + MCUboot
签名镜像。它们把 AFE、I-T/CV 方法、电位、时序、采样率、量程、扫描范围、扫描速度、
圈数和 EIS 档位留给运行时协议；点击“应用条件”时，软件只烧录随包镜像（首次或用户
明确重新应用时），开始测量前再把当前整组条件通过 RTT/USB 下发，并等待
`MEAS_CONFIRMED` 与物理寄存器回读完全匹配后才发送 `START`。

因此便携版的自定义条件不再需要 NCS、Zephyr、west、编译器或 SEGGER/J-Link 软件：
目标电脑只需解压/拖入应用即可修改条件、烧录、采集和测量。源码目录仍保留现场编译
路径，供固件开发和新增协议使用；它不是便携版的运行前置条件。通用固件构建入口为
`python packaging/build_runtime_firmware.py`，脚本会检查运行时配置确实传进应用子镜像。

macOS 的 libusb 可直接访问 J-Link，不使用 Windows 的 WinUSB 绑定机制，也不需要这项
驱动准备。macOS 打包验证会执行随包 OpenOCD，并检查其动态库和 scripts 都在 App 内。

打包和测试过程不会连接、复位或烧录任何板卡。
