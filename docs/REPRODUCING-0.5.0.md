# 0.5.0 干净克隆、复现与分发

## 复现范围

本仓库包含运行本地工作站所需的完整 Python、网页、3D 派生网格、测试、构建脚本、锁定依赖及 V4/V5.1 预编译固件。运行网页 / 模拟实验不需要原开发目录、Fusion、CadQuery、相邻仓库或开发者的 `.venv`。运行 DMG 不需要 Python、Node、Homebrew 或开发工具。

复现分为：同版本源码 / 依赖、软件测试与模拟结果、相同硬件上的实物验收。前两者可以在干净目录验证；实物测量受板卡、固件、装配、电极和溶液影响，不能承诺逐样本相同或完全没有 bug。安装包重新构建的时间戳 / UUID / 签名可不同，不承诺字节级可重建；下载包以 Release 的 SHA-256 校验。

## 下载即用

从 `v0.5.0` Release 下载 `SensUs-Workstation-macOS-arm64-0.5.0.dmg` 和 `SHA256SUMS.txt`。要求 Apple Silicon 与 macOS 14+，Intel Mac 不能使用这个 DMG。镜像内为自包含 App，拖入“应用程序”后启动。

本版 ad-hoc 签名，没有 Apple Developer ID 公证。首次打开若被 macOS 拦截，按系统“隐私与安全性”的正常流程人工允许打开；不要关闭系统安全功能。蓝牙需要开启系统开关并授予蓝牙权限，首次扫描需用户主动点击。

应用不会携带本机蓝牙绑定、串口、标定、实验数据或活动任务。首次使用必须在自己的电脑选择工作区、扫描设备、绑定 A/B、选择实际 RS485 端口，再进行本连接四点标定。

## 干净源码环境

已验证的源代码环境：CPython **3.12.10**、Node.js **22**。`packaging/source-test.lock` 固定全部测试 / 分析依赖及下载哈希；普通 `make install` 仍可用于不锁版本的开发，不等同于发布复现环境。

macOS 示例，需预先安装 Git、uv 与 Node.js 22：

```bash
git clone git@github.com:Terry-Wang-Lynx/SensUs-Electrochem-Workstation.git
cd SensUs-Electrochem-Workstation
git checkout v0.5.0
git rev-parse HEAD
uv python install 3.12.10
make install-locked PYTHON="$(uv python find 3.12.10)"
make test
make firmware-test
make package
SENSUS_TRANSPORT=ble .venv/bin/python -m pa_host.gui_server --port 8765
```

打开 `http://127.0.0.1:8765/`，先用“模拟实验”。仅运行页面或模拟不会验证实物精度。首次源码安装需要网络下载锁定包；3.12.10 以外的版本不会被 `install-locked` 冒充验证环境。固件纯逻辑测试还需系统 C 编译器 / make；不等于重新编译完整 Zephyr 镜像。

在 macOS26.6.2 + Apple clang17 的已知组合中，AddressSanitizer运行时会在项目 `main` 之前自旋，最小hello程序也能复现；不是把项目测试失败当通过。该环境用已验证的 Homebrew LLVM22.1.6 运行同一套测试：

```bash
make firmware-test CC="$(brew --prefix llvm)/bin/clang"
```

普通与ASan/UBSan均验证5538项断言。若没有LLVM22，先安装匹配的 `llvm` 工具链；不要把卡住的Apple运行时当作测试完成。Linux CI继续用系统编译器执行同样的正常和sanitizer测试。

Windows 源码复现使用同样的 Python 3.12.10 和 Node 22：

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
python -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r packaging/source-test.lock
.venv\Scripts\python -m pip install --no-deps --no-build-isolation -e .
.venv\Scripts\python -m pytest -q software/host/tests
.venv\Scripts\python -m pa_host.gui_server --transport ble --port 8765
```

## 自己构建 DMG

还需要 Apple Silicon、macOS 14+、Xcode Command Line Tools、make、curl、tar、`pkg-config` 与网络。可用系统的 `xcode-select --install` 安装命令行工具，并用 Homebrew 安装 `pkgconf`；Node 22 用于源代码测试，不属于最终 App 运行依赖。

```bash
xcrun --find swiftc
pkg-config --version
make dmg PYTHON="$(uv python find 3.12.10)"
```

脚本只接受 Python 3.12.10，使用 `packaging/portable-macos.lock` 哈希锁，单独创建构建环境，下载并校验固定版本 OpenOCD / libusb 源码。输出在 `artifacts/releases/0.5.0/`，其中 DMG 是分发物；`make app` 生成依赖源码 `.venv` 的开发应用，不能替代 DMG 发给别人。

Windows ZIP 必须在 Windows / 对应 GitHub Actions 上原生构建，不能从 Mac 伪造 Windows 验收。两平台便携环境均包含 SciPy，避免开发机使用 Welch、便携包却静默退化为不同 PSD 算法。Python 依赖许可证与 SBOM、OpenOCD / libusb 对应源码和许可证均随包提供。

## 安全复现真实设备

1. V5.2 板需预先烧录 `v52-0.1` 固件，NUS 服务与 INFO 验证通过才采集。本仓库不提供 V5.2 空片烧录、CV/EIS 或 OTA。
2. 扫描并用本机返回的身份绑定：远离 Z 电机的前板 A 对应 A–D，后板 B 对应 E–H。macOS 蓝牙 UUID 不能从开发者电脑照抄到另一台电脑。
3. 两电机共用 USB–RS485，X 地址1、Z地址2、115200/8N1、固定6B Emm私有协议，不是 Modbus。当前机构标称导程2mm/转，方向0为X左/Z下；其它机构必须重新核验，不盲用这些参数。
4. 每次连接进入 WASD / 方向键标定，分别对齐物理第1列X、第12列X、工作Z、安全Z。不要复制旧上电周期原始计数，不手拧后猜测电机零点，不省略现场余量 / 急停检查。
5. 先验证单列、逐通道身份与已知负载，再做完整路径。自定义 `[1,3,10,12]` 保留顺序；重复N轮是总共N轮，最后停在末列安全高度。每列沿用自己的设置，不因模式切换变成统一时长。
6. 检查原始 / 有效样本、缺口、轮次 / 路径身份和 manifest 哈希。两板并非硬件同步，SHA256不是电化学特征算法，CH3精度与原约199mm高速端点修正后的长行程仍待实物验收。

V5.2 固件与 `software/ver5.2/PROTOCOL.md` 位于独立的私有 `pA-Converter` 仓库，需项目所有者另行授权；PCB与原始机械STEP也不在本仓库。这里的 3D 网格已完整签入；只有再生成网格才需要原 STEP / CadQuery。不要用旧 V4/V5.1 镜像烧 V5.2，也不要把私有硬件仓库无意合并到公开上位机仓库。

## 发布一致性

`pyproject.toml` 的 `[tool.sensus.release]` 指定本版为 `preview` / `manual`。标签仍触发两平台 CI 构建和冒烟，但 CI 不二次创建 / 覆盖人工校验的 Release 资源。发布者从最终提交构建、核对包内模块 / 网页、只读挂载 DMG 测试、写出 SHA256SUMS，再上传；下次改为 CI 发布须显式修改该设置。

已遇到问题的回归与剩余限制见 [KNOWN-ISSUES-0.5.0.md](KNOWN-ISSUES-0.5.0.md)。现场日志与测量数据不入 Git；重要验证结论写入发布记录，不把空白工作区冒充已有设备绑定或有效标定。
