# SensUs Electrochemistry Workstation

SensUs 是一套基于 MAX30131 与 nRF52833 的三电极电化学工作站软件。本仓库独立包含 Zephyr 固件、RTT 采集链路，以及面向实验人员的本地交互界面。

界面已经打通以下流程：

- 恒电位 I-T 实时采集，每一个原生点即时显示并追加保存
- 循环伏安 CV：电位上下限、扫描速度、循环圈数与扫描前静置时间可配置
- CV 波形以 1 mV 步进；标准图横轴为电位、纵轴为电流，每个原生点实时显示并保存
- 电位、时长、输出采样率、末段拟合窗口、FSR 与 offset 配置
- 候选标定点管理、选定范围拟合、标定曲线锁定
- 未知样品浓度预测，以及可选的过渡期漂移 bias 校正
- 按固定间隔自动测量，支持标定、稳定化和测试三种任务
- 每轮原始数据、固定频率数据、摘要、图和实验索引自动落盘
- 主机侧软件滤波：1--4 阶软件低通，支持自动/手动截止频率，以及仅显示或参与稳态分析/标定

## 一键启动

### macOS

macOS 上推荐使用原生悬浮窗口。首次构建一次：

```bash
make app
open “dist/SensUs Workstation.app”
```

App 同时提供完整工作站和迷你悬浮检测窗。点击主窗口标题栏的”画中画”按钮，或按
`Command-Shift-O`，即可切换到悬浮模式；悬浮窗只保留实时电流、逐点曲线、进度、
样品信息与开始/停止按钮，并可显示在 macOS 全屏视频上方。点击悬浮窗标题栏的
“展开”按钮即可回到完整工作站。

主窗口标题栏图钉可以取消或恢复置顶；迷你检测窗始终置顶，但只占右上角的一小块
区域，不拦截窗口外的视频操作。App 会复用已经运行的工作站服务，没有服务时才从
本项目的 `.venv` 启动，因此不会因窗口切换而重启硬件。关闭窗口只会隐藏 App，
不会中断后台测量；点击 Dock 图标可以重新显示。

也可以双击 [Launch_Electrochem_Workstation.command](Launch_Electrochem_Workstation.command) 使用浏览器界面。首次启动会在仓库内创建 `.venv` 并安装 Python 包，随后打开：

```text
http://127.0.0.1:8765/
```

也可以从终端启动：

```bash
make install
make run
```

构建并打开 macOS App：

```bash
make app
open “dist/SensUs Workstation.app”
```

### Windows

双击 **`Launch_Electrochem_Workstation.bat`** 即可启动。首次运行会自动创建
Python 虚拟环境并安装依赖，然后打开浏览器进入工作站界面。

或者使用命令行：

```cmd
make -f Makefile.win install
make -f Makefile.win run
```

构建独立 Windows EXE（需要 PyInstaller）：

```cmd
windows\build_win.bat
```

详见 [windows/README.md](windows/README.md)。Windows 版本通过浏览器界面提供
全部核心功能（I-T/CV 采集、标定、分析、预测），但不包含 macOS 的原生悬浮窗。

App 直接加载本分支的完整工作站页面，并提供置顶主窗口和可选悬浮检测窗。
DEBUG 分支与 App 外壳的边界、硬件换算约束和推荐配置见
[docs/DEBUG_APP_INTEGRATION.md](docs/DEBUG_APP_INTEGRATION.md)。

硬件控制模式需要从完整源码仓库运行，因为应用条件时会现场生成配置、编译 Zephyr 固件并烧录。纯数据分析与界面资源也可从 wheel 安装。

## 实验流程

1. 在“实时测量”页选择独立工作目录，并在右侧选择 I-T 或 CV、应用检测条件。
2. 将样品标记为“标定”，连续采集多个候选点。
3. 进入“标定”，只选择需要的连续范围或单独点，生成并锁定测试曲线。
4. 如需稳定化，运行定时 I-T，并选择是否把起止漂移作为 bias 引入模型。
5. 将样品标记为“测试”，测量完成后自动套用锁定曲线预测浓度。

CV 不进入 I-T 标定/浓度预测链。扫描结束后会生成原始 CSV、标准化 CV CSV、
质量汇总 JSON 和曲线 PNG。默认条件为 `-0.6 至 +0.6 V`、`0.05 V/s`、
`30` 个完整循环、`2 s` 静置和 `40 µA` EIS ADC 量程；30 圈完整扫描约需
24 分钟。I-T 可在 DC ADC 的 `50 nA 至 2 µA` 与 EIS ADC 的
`4 至 40 µA` 量程之间选择。

测量不会自动跳到标定页。原始点在收到时就写入 `样品名称-已知浓度-raw.csv`；分析数据、摘要与图在一轮结束后立即生成。重复名称自动增加 `-r2`、`-r3`，不会覆盖历史数据。

## 软件滤波

“实时测量”和“硬件 DEBUG”页共用同一套主机侧滤波设置，点击“保存滤波设置”后
立即用于图表和实时电流显示。实时预览与结束后的分析使用相同的重复一阶前后向
算法和边界填充，因此同一组输入会得到一致结果。它不改写固件、MAX30131 的量程
或恒电位环，也不会修改原始采集文件。

- **关闭**：图表、实时读数和稳态统计都使用原始数据。
- **仅显示**：图表和实时读数使用滤波派生值；稳态统计、标定点和预测仍使用原始数据。
- **显示 + 稳态分析/标定**：图表和后端稳态统计使用同一套滤波；原始文件保留，
  另写出 `resampled_10hz-filtered.csv`，导出到实验目录时对应文件名为 `*-filtered.csv`。

低通支持 1--4 阶、自动或手动截止频率。滤波只作用于连续有效段，饱和点和 NaN
不会把前后数据串在一起。设置保存在 `measurements/filter_settings.json`，每轮摘要
也记录实际采样率、有效截止频率和提示信息。

数字滤波必须满足奈奎斯特限制。当前 MAX30131 原生采样约 8.06 Hz，手动低通超出
安全上限时软件会限制到奈奎斯特频率的 90% 并在面板提示。每轮实际使用的
滤波配置会随摘要保存供审计；原始 CSV 始终保留，可以回溯或重新分析。

## 电位完整性

固件现在只在上电或应用新条件后初始化一次 AFE。后续测量通过 RTT `START`
命令触发，不再用 MCU 复位开始新一轮。测量间隙的电解池状态由
`idle` 模式明确决定；当前正式方法默认 `idle=2`（断开），不宣称在轮次之间
保持恒电位。

采集期间固件每秒只读审计 DACA、DACB、DAC 路由、参考源和系统控制寄存器：

- `POTENTIAL_AUDIT`：控制寄存器与目标电位一致
- `POTENTIAL_FAULT`：连续两次回读不一致，本轮立即停止且禁止进入标定/预测
- `IT_ABORTED`：收到新的开始或停止命令；立即结束旧轮次，不复位 AFE
- `CV_START` / `CV_DONE` / `CV_ABORTED`：CV 的机器可读生命周期标记

固件会在测量过程中持续读取控制命令。即使上位机在上一轮中途断开，下一次
点击开始也会先结束遗留轮次，再发出新的 `IT_START`，因此实时曲线不会因等待
旧轮次而保持空白。

这项审计能确认数字控制状态，但不能替代示波器对实际 `WE-RE` 模拟电压的直接测量。45 µM 历史数据的专门排查见 [docs/45uM-current-transition-analysis.md](docs/45uM-current-transition-analysis.md)。

## 测试与打包

```bash
make test
make firmware-test
make app
make package
make portable-macos
make dmg
```

`make app` 生成经过临时签名的通用 macOS 应用（Apple Silicon 与 Intel），
`make package` 生成 `dist/*.whl` 和包含固件源码、App 构建脚本与方法档案的
`dist/*.tar.gz`。GitHub Actions 会执行主机端测试、固件纯逻辑层测试，并构建
分发包。完整 Zephyr 固件仍需本机的 NCS/Zephyr 工具链与自定义板定义。

给其他同学使用时，优先分发 `artifacts/releases/<version>/` 下的便携包。macOS
便携包面向 Apple Silicon/macOS 13+，Windows 包面向 Windows 10/11 x64；两者不要求
安装项目 Python 环境。Windows 包由 `windows/build_portable.ps1` 或
`portable-release` GitHub Actions 在 Windows 上原生生成。稳定前端的签名更新、
缓存、重启激活和回退机制见
[docs/PORTABLE_DISTRIBUTION.md](docs/PORTABLE_DISTRIBUTION.md)。

## 目录

```text
software/host/pa_host/       Python 采集、分析、标定和 GUI 服务
software/host/pa_host/gui/   本地 Web 界面
software/firmware/           nRF52833 + MAX30131 Zephyr 固件
software/firmware/tests/     可在普通主机运行的 AFE 纯逻辑测试
protocols/                   受版本控制的实验方法与固件配置
docs/                        调试记录与排查报告
packaging/                   便携运行时、签名更新与跨平台发布脚本
```

固件和数据链路的详细说明见 [software/README.md](software/README.md)。

## 发布说明

仓库当前未声明覆盖全项目的开源许可证。上传公开 GitHub 仓库前，项目所有者需要选择并加入适用的许可证；在此之前可作为私有仓库或内部发布包使用。
