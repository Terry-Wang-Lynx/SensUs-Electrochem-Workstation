# SensUs 电化学工作站交接说明

本文是 `v0.4.12` 的维护入口，面向接手上位机、V4/V5.1 固件、打包和现场调试的开发者。实验人员只需从 [GitHub Releases](https://github.com/Terry-Wang-Lynx/SensUs-Electrochem-Workstation/releases/latest) 下载分发包。

## 1. 交接基线与仓库边界

- 权威仓库：`https://github.com/Terry-Wang-Lynx/SensUs-Electrochem-Workstation`
- 交接标签：`v0.4.12`
- 上位机版本：`0.4.12`
- 固件运行时协议：`MEAS/1`
- 固件镜像溯源点：提交 `14967a03f93df652719e7323a6bfcabeeba9f776`
- 标签对应提交：`git rev-list -n 1 v0.4.12`

该仓库包含完整的上位机、V4/V5.1 应用固件、板定义、主机与固件测试、打包脚本和发布时使用的预编译镜像。GitHub 标签页自动生成的 Source code ZIP/tar.gz 包含全部受 Git 跟踪的文件；需要完整历史时请 clone 仓库并 checkout `v0.4.12`。

该仓库不包含 KiCad PCB、LTspice 仿真、分版本硬件设计真值和 Board3_CLY 独立固件。它们属于父级 [clateral912/pA-Converter](https://github.com/clateral912/pA-Converter)（私有仓库，交接时需另行授权）或其他独立工程，不能把两个仓库混成一个 Git 提交。测量数据、标定数据、本机日志、密钥和临时构建目录也不属于源码。

仓库目前公开但没有覆盖全项目的 `LICENSE`。源码可见不等于已经授予第三方再分发或修改许可；项目所有者移交时应另外明确授权范围，或选择并提交合适的许可证。

## 2. 板卡、固件与连接方式

| 实验室称呼 | 硬件版本 | 日常传输 | 固件更新 | 上位机选择 |
| --- | --- | --- | --- | --- |
| 板 1 | V4.0，无板载 USB | J-Link/SWD + RTT | J-Link 安分页写入、校验、复位 | `J-Link xxxx` |
| 板 2 | V5.1，双 USB CDC | DATA CDC 配置/采集；SMP CDC 升级 | 已初始化板卡经 MCUboot/SMP 纯 USB 更新 | `USB xxxx` |

V4 和 V5.1 共用 MAX30131 方法、换算、审计和 `MEAS/1` 控制源码，但板定义、启动方式和主机传输不同。日常操作必须按上表选择：V4 用 J-Link/RTT，V5.1 用 USB DATA/SMP。

V5.1 空片没有可用的工厂 USB bootloader，首次写入 MCUboot、应用和 UICR 必须使用 J-Link/SWD。此后的参数修改、测量和签名应用升级可以只接 USB。V5.1 的 J-Link 路径用于首次引导、恢复和固件调试；当前上位机不能仅凭 J-Link 自动区分 V4 与 V5.1，因此不要在未确认板型时用 V4 日常烧录路径覆盖 V5.1。

同时连接多个 J-Link、多个 USB 板或混合设备时，右上角设备列表会要求手动选择。设备显示名称来自真实枚举结果，不按连接方式硬编码。

## 3. 随包固件真值

固件没有独立的 semantic version；交接时以目标板、`MEAS/1`、文件 SHA-256 和 Git 溯源点共同标识。下面的哈希已与仓库实际文件核对。

### V4.0 / 板 1

- target：`pa_converter_v40/nrf52833`
- manifest：`software/firmware/prebuilt/firmware.json`
- RTT control block：`0x20001100`
- `software/firmware/prebuilt/zephyr.hex`
  - SHA-256：`b582d15fe9f8c04f4e08f1a3fddcd265d988df9c5845e8f750f47081346e7206`
- `software/firmware/prebuilt/zephyr.elf`
  - SHA-256：`f9ebc18e13dca85ae37842a36fc8e8772e49cf50d76c6a09a4847b730ef40130`

### V5.1 / 板 2

- target：`pa_converter_v51/nrf52833`
- manifest：`packaging/resources/v51/firmware.json`
- `packaging/resources/v51/images/app.signed.bin`
  - SHA-256：`7c5a8cb055a31d472cf1c9aa263d8059f48fae526af719490eb28bf5509ea619`
- `packaging/resources/v51/images/app.signed.hex`
  - SHA-256：`686d60e5e9ffab3836c6be43adc6db74c74908ec33fc0748b86f296e50e6c181`
- `packaging/resources/v51/images/dfu_application.zip`
  - SHA-256：`2c3e28a8065900e9c9083443467b19854e5b36c69a8c59fe9aca448421799af9`
- `packaging/resources/v51/images/mcuboot.hex`
  - SHA-256：`2cb22f495c2739a24faef222b7817048f270036ba12a8ed040e9e137a3d136dd`

V5.1 的 MCUboot 从 `0x00000` 开始，slot 0 从 `0x18000` 开始。UICR 必须保持 `REGOUT0.VOUT=3.3 V (5)`、`APPROTECT.PALL=0x5a`，REG1 DC/DC 必须禁用。常规更新禁止 whole-chip/mass erase，否则可能擦除这些板级设置。当前 PCB 的 USB-C 只有一个插头方向能枚举，USB 不出现时先翻转插头再诊断。

父级硬件仓库中的旧 V5.1 SOP 和 `software/ver5.1/images/` 只作历史参考，其镜像哈希与本节 `v0.4.12` 工件不同，不能用于当前交接版。允许分发或写入的镜像只能来自本仓库 `v0.4.12` 标签，并且必须与本节六个 SHA-256 一致。权威仓库已移除引用本机 SEGGER V8.80 和不存在 UICR 脚本的旧手工 helper；V5.1 日常更新由上位机直接调用随包 `smpmgr`，空片/UICR 恢复必须由维护者依据当前硬件真值单独执行和复核。

## 4. 当前方法条件

两套镜像都是 runtime configurable。便携版不会为每组实验参数重新编译固件；它通过 `MEAS/1` 下发整组 I-T/CV 条件，并要求带请求 ID 的 `GET` 回读、`MEAS_CONFIRMED` 和 MAX30131 物理寄存器审计全部通过后才发送 `START`。

随包 manifest 的 I-T 默认条件是 `+0.2 V`、`180 s`、目标输出 `10 Hz`、末段 `20 s`、`FSR=2000 nA`、`offset=200 nA/10%`。硬件原生最快约 `8.06 Hz`，上位机按时间戳重采样为 10 Hz；不要把输出频率误当成 10 个独立硬件样本每秒。

界面提供四套快速预设（两种电极 × 两个工作电位；电极维度只改量程，偏置一律 `20% FSR`）。点预设只填入表单，仍需点“应用条件”完成硬件核验：

| 预设 | E | V_WE | FSR | offset | 时长 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `微针 +0.2V` | `+0.2 V` | `1.2 V` | `50 nA` | `10 nA` | `180 s` |
| `丝网印刷 +0.2V` | `+0.2 V` | `1.2 V` | `1 µA` | `200 nA` | `180 s` |
| `微针 -0.2V` | `-0.2 V` | `0.25 V` | `50 nA` | `10 nA` | `120 s` |
| `丝网印刷 -0.2V` | `-0.2 V` | `0.25 V` | `1 µA` | `200 nA` | `120 s` |

软件滤波只影响显示及可选的稳态分析，不改写原始 CSV 或硬件条件。设置修改后自动保存。

## 5. 源码目录职责

```text
software/host/pa_host/       Python 后端、采集、分析、标定和诊断
software/host/pa_host/gui/   本地 Web 前端
software/host/tests/         主机、界面契约、连接和打包测试
software/firmware/src/       两板共用应用与控制传输
software/firmware/lib/       MAX30131 驱动、配置和换算
software/firmware/boards/    V4/V5.1 自定义 Zephyr 板定义
software/firmware/sysbuild/  V5.1 MCUboot 配置
software/firmware/prebuilt/  V4 随包镜像
packaging/resources/v51/     V5.1 随包镜像与 USB 更新资源
packaging/                   固件重建、冻结依赖和跨平台打包
macos/                       AppKit/WebKit 原生外壳
windows/                     Windows 启动器和便携包构建
protocols/                   受版本控制的方法档案
docs/                        分发、诊断、分析和交接文档
```

`software/firmware/west.yml` 固定 `sdk-nrf v3.4.0`，但当前工程实际采用 freestanding application：NCS 工作区在仓库外，应用源码留在本仓库。不要把数 GB 的 NCS/Zephyr 第三方源码复制进项目。

## 6. 开发和测试环境

### 上位机

- 源码最低版本：Python `>=3.10`
- Release 冻结运行时：CPython `3.12.10`
- Release JavaScript 检查：Node.js `22`
- 关键冻结依赖：PyInstaller `6.22.2`、numpy `2.5.2`、matplotlib `3.11.1`、pyserial `3.5`、smpmgr `0.18.1`
- 精确 Python 依赖与哈希：`packaging/portable-macos.lock`、`packaging/portable-windows.lock`

### 固件

- nRF Connect SDK：`v3.4.0 LTS`
- Zephyr：`4.4.0`
- Zephyr SDK：`1.0.1`
- arm-zephyr-eabi GCC：`14.3.0`
- 已验证 west：`1.5.0`；安装脚本当前未锁死 west 版本，重现旧构建时应显式安装 `west==1.5.0`

### 随包本机工具

- OpenOCD `0.12.0`，仅启用 J-Link/libjaylink，配套 libusb `1.0.29`
- Windows WinUSB helper：libwdi `1.5.1`，固定提交 `9b23b82a2dd1cbffc16d46c212f92c6bf8c0c602`
- Windows helper 同时携带对应的 WDK 8.0、libusb-win32 `1.4.0.0`、libusbK `3.1.0.0` 源码/条款
- 每个便携包携带组件 manifest、对应源码、许可证和 Python SBOM

Windows 只有在应用确认受支持的 J-Link 调试接口驱动异常、用户主动点“准备 J-Link”并确认 UAC 后才运行 WinUSB helper。macOS 直接通过 libusb 访问 J-Link，没有 WinUSB 步骤。兼容 SEGGER Commander 仅作为特定旧探头的备用通道，不是全局必装依赖，也不要求固定到历史 V8.80。

## 7. 从源码运行与验证

在仓库根目录执行：

```bash
make install
make test
make firmware-test
make package
```

`make test` 运行 pytest、Python compileall 和两个前端 JavaScript 语法检查。`make firmware-test` 使用 `-Wall -Wextra -Werror` 构建固件纯逻辑测试，并执行正常版和 ASan/UBSan 版。`make package` 生成 wheel 和 sdist；它们用于 Python 分发，不等于完整 Git 仓库快照。

源码模式启动：

```bash
make run
```

Windows 也可双击 `Launch_Electrochem_Workstation.bat`；macOS 也可双击 `Launch_Electrochem_Workstation.command`。`make app` 生成依赖本地 `.venv` 的 universal 开发壳，不是 Release 的零依赖 DMG。

## 8. 重建两板固件资源

先准备 NCS v3.4.0 和 Zephyr SDK 1.0.1，然后从仓库根目录执行：

```bash
python packaging/build_runtime_firmware.py \
  --ncs-dir "$HOME/sensus-toolchains/ncs" \
  --sdk-dir "$HOME/sensus-toolchains/zephyr-sdk-1.0.1"
```

如果工具链位于历史默认位置，则改为 `--ncs-dir "$HOME/ncs" --sdk-dir "$HOME/zephyr-sdk-1.0.1"`。必须显式写路径，因为 `03-安装固件工具链.command` 与构建脚本默认值目前不同。

脚本会 always-clean 构建 V4 和启用 MCUboot 的 V5.1，检查运行时配置确实进入 application child image，随后更新预编译镜像和 manifest 哈希。提交新镜像前至少检查：

```bash
shasum -a 256 \
  software/firmware/prebuilt/zephyr.hex \
  software/firmware/prebuilt/zephyr.elf \
  packaging/resources/v51/images/*
make firmware-test
```

修改固件协议、板定义或预编译镜像时，要同时更新本文件的固件哈希和溯源说明。不要只复制二进制而不提交生成它的源码。

## 9. 分发包与发布流程

Release 支持：

- Windows 10/11 x64：自包含 ZIP；没有 WebView2 Runtime 时退回系统浏览器
- Apple Silicon + macOS 14 及以上：自包含 arm64 DMG
- 当前不提供 Intel Mac DMG、Windows ARM 包

本地构建前置：macOS 需要 Apple Silicon、macOS 14+、Xcode Command Line Tools、Python `3.12.10`、Node.js `22`、Homebrew `pkgconf` 和网络连接。随后执行：

```bash
brew install pkgconf
make dmg VENV_PYTHON=python PYTHON=python
```

构建脚本对 Python 版本是硬校验（`3.12.10`，不匹配直接退出）。这个版本决定打进包里的解释器与
`packaging/portable-macos.lock` 的依赖集，不要放宽。若本机是别的版本，取一个独立的 3.12.10
即可，不必改动系统 Python：

```bash
uv python install 3.12.10
UVPY="$(uv python find 3.12.10)"
make dmg VENV_PYTHON="$UVPY" PYTHON="$UVPY"
```

**带宽受限时先预取依赖再离线构建（2026-08-19 实测踩过）。** lock 里有 matplotlib、numpy
这类大包；在 ~110 kB/s 的链路上，`pip install` 会在某个大文件上耗尽读超时，导致整个构建
在中途失败——即便已经调大 `PIP_TIMEOUT` / `PIP_RETRIES` 也一样。先把整份 lock 抓成本地
wheelhouse（可反复重跑，已下载的自动跳过），再让构建完全离线：

```bash
"$UVPY" -m pip download --require-hashes -r packaging/portable-macos.lock \
  -d /tmp/wheelhouse --timeout 300 --retries 30
PIP_NO_INDEX=1 PIP_FIND_LINKS=/tmp/wheelhouse \
  make dmg VENV_PYTHON="$UVPY" PYTHON="$UVPY"
```

`--require-hashes` 在预取阶段就逐包校验，所以离线安装的安全性没有打折；也因此**没有必要换
镜像源**。⚠️ 排查网络时不要手写 `files.pythonhosted.org` 的 wheel URL——真实路径带 hash
前缀，手写必然 404，会被误读成"仓库不可达"。要测连通性请从 `https://pypi.org/pypi/<包>/<版本>/json`
取真实 URL。

发版还需同步**三处**版本号，`test_release_versions_stay_in_sync` 会校验：`pyproject.toml`、
`software/host/pa_host/__init__.py`、`macos/Info.plist`（`CFBundleShortVersionString` 与
`CFBundleVersion` 两个键）。

Windows 的零依赖 ZIP 不能只运行 `build_portable.ps1`：干净构建机还要先生成 WinUSB helper 和 OpenOCD。准备 Git、Python `3.12.10`、Node.js `22`、Visual Studio 2022/MSBuild、7-Zip，以及 MSYS2 MINGW64 的 `autoconf automake libtool make mingw-w64-x86_64-gcc mingw-w64-x86_64-pkgconf`。在 Visual Studio Developer PowerShell 中执行 helper：

```powershell
./packaging/build_windows_winusb_helper.ps1
```

在 MSYS2 MINGW64 shell 中从同一仓库执行：

```bash
./packaging/build_windows_openocd.sh artifacts/build/windows-x64/openocd
```

回到 Developer PowerShell 生成 ZIP：

```powershell
./windows/build_portable.ps1 -Python python
```

日常维护优先使用 GitHub Actions 的干净 runner；`.github/workflows/portable-release.yml` 是完整且经过验证的构建顺序真值。

正式发布只从已推送的干净 `main` 创建 annotated tag：

```bash
git fetch origin
git switch main
git pull --ff-only
make test
make firmware-test
git tag -a v0.4.12 -m "SensUs Electrochem Workstation v0.4.12"
git push origin v0.4.12
```

标签会触发 `.github/workflows/portable-release.yml`，并强制版本与 `pyproject.toml` 一致。工作流构建两端，检查冻结后端 GUI/collector 入口、health/version、OpenOCD adapter、依赖源码/许可证/hash/SBOM、DMG 和 ZIP 完整性，然后创建中文草稿 Release。草稿不会自动成为 latest；必须确认所有 job 成功、资产名称和 `SHA256SUMS.txt` 正确后再手动发布。

预期资产：

- `SensUs-Workstation-macOS-arm64-0.4.12.dmg`
- `SensUs-Workstation-Windows-x64-0.4.12.zip`
- `SHA256SUMS.txt`
- GitHub 自动生成的 Source code ZIP/tar.gz

当前两个 repository variable 均未配置：`SENSUS_FRONTEND_MANIFEST_URL`、`SENSUS_FRONTEND_PUBLIC_KEY`。因此独立的签名前端热更新处于禁用状态，应用使用包内前端；整包 GitHub Release 更新仍可用。详情见 `docs/PORTABLE_DISTRIBUTION.md`。

## 10. 数据、状态与诊断

正式测量前必须选择可写工作区。软件在工作区下为每个批次建立子目录，并保存原始 CSV、重采样/滤波派生文件、摘要、图、条件、滤波设置、标定模型和索引。工作区数据不进入 Git。

便携版状态和日志位置：

| 平台 | 状态 | 日志 |
| --- | --- | --- |
| macOS | `~/Library/Application Support/SensUs Workstation/` | `~/Library/Logs/SensUs Workstation/` |
| Windows | `%LOCALAPPDATA%\SensUs Workstation\` | `%LOCALAPPDATA%\SensUs Workstation\logs\` |

界面操作失败会显示诊断编号。“硬件 DEBUG”页可查看本会话设备、参数、烧录和测量事件，并下载已脱敏诊断包；诊断包不包含原始测量 CSV。排查问题时至少保留诊断编号、应用版本、设备序列号、连接方式、板卡供电方式和复现步骤。

## 11. 硬件安全和已知边界

- V4 J-Link 烧录会读取 FICR 并核验 nRF52833 身份，失败时不得绕过。V5.1 USB/SMP 更新无法读取 FICR；它依靠用户选定设备的 DATA/SMP 物理配对、实时协议探测和 MCUboot 签名镜像约束目标，因此多设备时必须确认所选 USB 板。
- J-Link 的 VTref 是目标电压参考，不应把不确定的电源拓扑当作软件问题；板卡、探头和电池同时供电前先确认硬件设计。
- 数字寄存器回读只能证明配置链路，不能代替示波器对 `WE-RE`、CE/WO headroom 和供电纹波的测量。
- macOS 包目前是 ad-hoc 签名且未 notarize，下载后首次需在 Finder 右键“打开”。
- Windows 包未做商业代码签名，可能出现 SmartScreen；WinUSB 准备会出现一次 UAC。
- CI 没有实体硬件。`v0.4.12` 已做软件和包级验证，但真实 V4 J-Link、V5.1 USB 的烧录、参数下发、首点时延和完整测量必须在插板后验收。
- 启动优化目标是把已就绪设备的必要配置路径压到约 2 秒；实际时间受 USB 枚举、探头固件、Windows Defender 和板卡状态影响。优化没有移除条件回读或物理核验门禁。

## 12. 接手人首日验收清单

1. clone 仓库，checkout `v0.4.12`，记录 `git rev-parse HEAD`。
2. 核对六个固件文件 SHA-256 与第 3 节一致。
3. 执行 `make install && make test && make firmware-test`。
4. 在 Windows 10/11 x64 冷机解压 ZIP，验证启动、退出、重开和 SmartScreen/UAC 流程。
5. 在 Apple Silicon + macOS 14+ 安装 DMG，验证首次右键打开、退出和重开。
6. 板 1/V4：J-Link 识别、应用条件、读回、开始/停止 I-T，并检查原始 CSV。
7. 板 2/V5.1：仅接 USB，验证 DATA/SMP 识别、参数下发、应用升级和 I-T。
8. 同时连接两个设备，确认必须手动选择且不会向另一块板烧录。
9. 用四套快速预设各做一次参数回读；必要时再做 CV。
10. 测量期间插拔 MacBook 电源、重插设备、关闭并重开应用，确认前端不卡死且无残留 backend/OpenOCD。
11. 新建工作区和两个命名批次，验证历史记录可切换并恢复界面、条件、滤波和曲线。
12. 记录首点时延与诊断包；真机全部通过后，把结果和板卡序列号写入新的验收记录。
