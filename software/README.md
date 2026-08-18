# software/：V4/V5.1 固件与上位机入口

本目录是 SensUs 电化学工作站的软件真值，当前交接版本为 `v0.4.12`。完整版本、工具链、固件哈希、发布和真机验收说明见 [`docs/HANDOFF.md`](../docs/HANDOFF.md)。

## 当前状态

V4 和 V5.1 已共用同一套 MAX30131 测量方法和 `MEAS/1` 运行时配置协议，上位机已支持 I-T、CV、标定、预测、自动测量、工作区批次恢复、历史曲线、软件滤波和诊断包。旧文档中“待首烧”“只能使用 SEGGER J-Link V8.80”等内容是早期 bring-up 记录，不再是当前全局前置条件。

| 板卡 | 目标 | 日常连接 | 更新方式 |
| --- | --- | --- | --- |
| 板 1 / V4.0 | `pa_converter_v40/nrf52833` | J-Link + RTT | J-Link/SWD 安分页写入、校验、复位 |
| 板 2 / V5.1 | `pa_converter_v51/nrf52833` | USB DATA + SMP | MCUboot/SMP 签名应用升级 |

V5.1 空片首次写入 MCUboot、应用和 UICR 仍需 J-Link/SWD；初始化后可仅用 USB 修改参数、更新应用和测量。V5.1 的 J-Link 用于首次引导、恢复和开发调试。当前仅从 J-Link 无法自动判断板型，不要在未确认板型时把 V4 日常镜像写入 V5.1。

## 目录

```text
software/
├── host/
│   ├── pa_host/                  Python 后端、采集、分析和 Web UI
│   │   └── gui/                  HTML/CSS/JavaScript 前端
│   └── tests/                    主机、硬件状态机和打包测试
└── firmware/
    ├── src/                      共用应用与控制传输
    ├── lib/max30131/             AFE 驱动、配置和换算
    ├── boards/senseus/           V4/V5.1 自定义板定义
    ├── boards/pa_converter_v51.conf  V5.1 双 CDC overlay
    ├── sysbuild/                 V5.1 MCUboot 配置
    ├── prebuilt/                 V4 随包 HEX/ELF 与 manifest
    └── tests/                    可在普通主机运行的 C 逻辑测试
```

V5.1 随包签名镜像和 MCUboot 位于仓库根目录的 `packaging/resources/v51/`。父级硬件仓库的旧 V5.1 SOP/镜像仅作历史参考，当前维护只允许使用本仓库标签和 `docs/HANDOFF.md` 中列出的 SHA-256。

## 数据与控制链

V4 使用包内 OpenOCD/libjaylink 建立 RTT server；V5.1 的 DATA CDC 承载相同的文本配置和样本协议，SMP CDC 只用于 MCUboot 升级。上位机开始正式测量时执行：

```text
选择并锁定物理设备
  -> 启动 collector
  -> SET <带 request id 的整组条件>
  -> GET <同一 request id>
  -> 核对 MEAS_CONFIRMED 和 MAX30131 物理寄存器
  -> START
  -> 原生样本逐点落盘和显示
```

任一配置或物理回读不匹配时不会发送 `START`。采集器会记录 `measurement.start_ready` 各阶段耗时，便于区分设备枚举、collector 启动、配置门禁和首点延迟。

MAX30131 的 `SENS_PERIOD` 最短约 124 ms，原生采样约 8.06 Hz。上位机可以按时间戳重采样为严格 10 Hz，但不会把插值点伪装成独立硬件样本。原始点在收到时立即追加到 `*-raw.csv`；停止或异常不会删除已经写入的数据。

固件测量期间持续输出电位审计：

- `POTENTIAL_AUDIT`：数字控制寄存器与目标一致
- `POTENTIAL_FAULT`：连续两次不一致，本轮立即停止且不进入标定/预测
- `IT_START` / `IT_DONE` / `IT_ABORTED`：I-T 生命周期
- `CV_START` / `CV_DONE` / `CV_ABORTED`：CV 生命周期

数字审计不能代替示波器对实际 `WE-RE`、CE/WO headroom 和供电的测量。

## 当前条件与预设

随包运行时默认是 `+0.2 V`、`180 s`、目标输出 `10 Hz`、末段 `20 s`、`FSR=2000 nA`、`offset=200 nA/10%`。默认条件只是可启动基线，不代表每种化学体系的推荐量程。

界面的快速预设来自已确认的历史测量：

| 预设 | E | V_WE | FSR | offset |
| --- | ---: | ---: | ---: | ---: |
| `+0.4V 氧化` | `+0.4 V` | `1.2 V` | `50 nA` | `9 nA` |
| `-0.2V 还原` | `-0.2 V` | `0.25 V` | `100 nA` | `80 nA` |

选择预设只会填表，用户仍需点击“应用条件”。应用后后端会读取硬件确认，不能用 UI 中的数值替代物理回读。

主机侧滤波支持 1--4 阶低通、自动/手动截止频率，以及“仅显示”或“显示 + 稳态分析/标定”。修改后自动保存。原始 CSV 永远保留，滤波结果写入独立派生文件。

## 工作区

首次正式测量前必须选择一个存在且可写的工作区。软件记住该根目录，每个新批次在其下创建独立子目录，并保存：

- 原始与重采样 CSV
- 可选滤波 CSV
- 摘要 JSON、图和质量信息
- 方法条件、滤波和稳态设置
- 标定候选点、锁定模型和实验索引

历史记录只列出当前工作区的批次。打开历史批次会恢复当时的条件、滤波、标定和曲线；不会把数据复制到应用临时目录。实验数据和本机状态不提交 Git。

## 源码运行

在仓库根目录：

```bash
make install
make run
```

也可以直接使用命令行入口：

```bash
PYTHONPATH=software/host .venv/bin/python -m pa_host.collect --help
PYTHONPATH=software/host .venv/bin/python -m pa_host.it_tool --help
PYTHONPATH=software/host .venv/bin/python -m pa_host.gui_server --open-browser
```

V5.1 手动采集时只把 DATA CDC 传给 collector，不能把 SMP CDC 当作数据端口：

```bash
PYTHONPATH=software/host .venv/bin/python -m pa_host.collect \
  --serial /dev/cu.usbmodemDATA \
  --out run.csv \
  --raw-log run.usb.log
```

V4 的 RTT/OpenOCD 路径和所有正式测量参数以 `pa_host.gui_server` 生成的命令为准，避免手工遗漏目标身份、探头序列号或配置门禁。

## 固件开发

固定开发基线：NCS `v3.4.0 LTS`、Zephyr `4.4.0`、Zephyr SDK `1.0.1`、已验证 west `1.5.0`。`software/firmware/west.yml` 是版本参考；实际使用仓库外的 freestanding NCS workspace。

一次重建 V4/V5.1 所有随包资源：

```bash
python packaging/build_runtime_firmware.py \
  --ncs-dir "$HOME/sensus-toolchains/ncs" \
  --sdk-dir "$HOME/sensus-toolchains/zephyr-sdk-1.0.1"
```

脚本对两个 target 都执行 always-clean build，并检查 runtime 配置进入 V5.1 application child image。提交固件时必须同时提交源码、板定义、manifest 和对应镜像，核对 manifest 中的 SHA-256。

V5.1 板级安全条件：

- MCUboot `0x00000`，slot 0 `0x18000`
- UICR `REGOUT0.VOUT=5`（3.3 V）
- UICR `APPROTECT.PALL=0x5a`
- REG1 DC/DC 禁用，因为 PCB 未装对应电感
- 常规更新禁止 mass erase
- USB-C 当前只有一个插头方向能枚举

## 测试

```bash
make test
make firmware-test
make package
```

`make test` 覆盖后端、前端 DOM 契约、USB/J-Link 状态机、工作区、诊断、打包和更新逻辑，并执行 Python/JavaScript 语法检查。`make firmware-test` 使用 `-Wall -Wextra -Werror`，同时运行普通和 ASan/UBSan 测试。

发布前还需由 GitHub Actions 在 Windows/macOS 原生 runner 上构建自包含 ZIP/DMG，并执行冻结后端、collector、OpenOCD、资源哈希、许可证和包完整性冒烟测试。CI 不接实体板，真机验收清单见 [`docs/HANDOFF.md`](../docs/HANDOFF.md#12-接手人首日验收清单)。

## 日志与故障定位

- macOS 状态：`~/Library/Application Support/SensUs Workstation/`
- macOS 日志：`~/Library/Logs/SensUs Workstation/`
- Windows 状态：`%LOCALAPPDATA%\SensUs Workstation\`
- Windows 日志：`%LOCALAPPDATA%\SensUs Workstation\logs\`

页面报错时记录诊断编号，并从“硬件 DEBUG”下载诊断包。J-Link 在线但目标未响应时，优先检查板端供电、VTref、SWDIO、SWCLK、GND 和接口方向；USB 未枚举时确认数据线、翻转 V5.1 USB-C 插头并等待 DATA/SMP 两个接口恢复。不要用修改代码掩盖真实供电或接线问题。
