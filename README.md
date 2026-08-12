# SensUs Electrochemistry Workstation

SensUs 是一套基于 MAX30131 与 nRF52833 的三电极电化学工作站软件。本仓库独立包含 Zephyr 固件、RTT 采集链路，以及面向实验人员的本地交互界面。

界面已经打通以下流程：

- 恒电位 I-T 实时采集，每一个原生点即时显示并追加保存
- 电位、时长、输出采样率、末段拟合窗口、FSR 与 offset 配置
- 候选标定点管理、选定范围拟合、标定曲线锁定
- 未知样品浓度预测，以及可选的过渡期漂移 bias 校正
- 按固定间隔自动测量，支持标定、稳定化和测试三种任务
- 每轮原始数据、固定频率数据、摘要、图和实验索引自动落盘

## 一键启动

macOS 上双击 [Launch_Electrochem_Workstation.command](Launch_Electrochem_Workstation.command)。首次启动会在仓库内创建 `.venv` 并安装 Python 包，随后打开：

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
open "dist/SensUs Workstation.app"
```

App 直接加载本分支的完整工作站页面，并提供置顶主窗口和可选悬浮检测窗。
DEBUG 分支与 App 外壳的边界、硬件换算约束和推荐配置见
[docs/DEBUG_APP_INTEGRATION.md](docs/DEBUG_APP_INTEGRATION.md)。

硬件控制模式需要从完整源码仓库运行，因为应用条件时会现场生成配置、编译 Zephyr 固件并烧录。纯数据分析与界面资源也可从 wheel 安装。

## 实验流程

1. 在“实时测量”页选择独立工作目录，并在右侧应用 I-T 条件。
2. 将样品标记为“标定”，连续采集多个候选点。
3. 进入“标定”，只选择需要的连续范围或单独点，生成并锁定测试曲线。
4. 如需稳定化，运行定时 I-T，并选择是否把起止漂移作为 bias 引入模型。
5. 将样品标记为“测试”，测量完成后自动套用锁定曲线预测浓度。

测量不会自动跳到标定页。原始点在收到时就写入 `样品名称-已知浓度-raw.csv`；分析数据、摘要与图在一轮结束后立即生成。重复名称自动增加 `-r2`、`-r3`，不会覆盖历史数据。

## 电位完整性

固件现在只在上电或应用新条件后初始化一次 AFE。后续测量通过 RTT `START` 命令触发，测量间隙持续保持配置电位，不再用 MCU 复位开始新一轮。

采集期间固件每秒只读审计 DACA、DACB、DAC 路由、参考源和系统控制寄存器：

- `POTENTIAL_AUDIT`：控制寄存器与目标电位一致
- `POTENTIAL_FAULT`：连续两次回读不一致，本轮立即停止且禁止进入标定/预测
- `IT_ABORTED`：收到新的开始或停止命令；立即结束旧轮次，不复位 AFE

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
```

`make package` 生成 `dist/*.whl` 和包含固件源码的 `dist/*.tar.gz`。GitHub Actions 会执行主机端测试、固件纯逻辑层的 178 项断言，并构建分发包。完整 Zephyr 固件仍需本机的 NCS/Zephyr 工具链与自定义板定义。

## 目录

```text
software/host/pa_host/       Python 采集、分析、标定和 GUI 服务
software/host/pa_host/gui/   本地 Web 界面
software/firmware/           nRF52833 + MAX30131 Zephyr 固件
software/firmware/tests/     可在普通主机运行的 AFE 纯逻辑测试
docs/                        调试记录与排查报告
```

固件和数据链路的详细说明见 [software/README.md](software/README.md)。

## 发布说明

仓库当前未声明覆盖全项目的开源许可证。上传公开 GitHub 仓库前，项目所有者需要选择并加入适用的许可证；在此之前可作为私有仓库或内部发布包使用。
