# DEBUG 分支 App 整合说明

## 目的与基线

本 App 以 `feat/afe-runtime-config-vdd-debugpage` 分支的提交
`2603bd9` 及其后续修复为硬件与采集基线。整合时保留该分支的 AFE 配置、
运行时审计和电位诊断作为权威实现，并在同一链路上接入完整工作站、CV 和
macOS 悬浮窗口。

整合后仍以 DEBUG 分支的下列核心实现为准：

- `software/firmware/` 下的 MAX30131 配置、运行时命令、System ADC 与 VDD 采集
- `software/host/pa_host/collect.py` 的 J-Link/RTT 采集链路
- `software/host/pa_host/gui_server.py` 的正式测量与硬件 DEBUG API
- `software/host/pa_host/gui/` 的硬件 DEBUG 参数与运行状态解释

在此基线上新增了 macOS App 外壳、悬浮窗、重排的工作站界面与 CV
功能。CV 使用专用 EIS ADC 宽量程；DOPA I-T 默认仍使用已验证的
2 uA DC ADC 方法，不会因 CV 整合而切换到宽量程通道。

## 为什么这样整合

DEBUG 分支已经把运行时 AFE 配置、实际生效参数回读、配置审计、双轴电流/电位
曲线、System ADC 和 VDD 观测放在同一套固件与采集链路中。直接保留这条链路，
可以避免 App 启动后意外切回旧版固件或旧的参数解释。

App 会优先读取构建时写入的 `project-root.txt`，并校验 `/api/health` 返回的
`project` 是否与该目录完全一致。因此，即使电脑中还保存着旧版 App 的路径，
本 App 也不会把另一个仓库中运行在 `8765` 端口的服务误认为当前 DEBUG 版本。

## 电流换算与实验约束

随附的 AFE-v4 理论框架将 `counts` 定义为唯一原始观测量，并给出：

```text
IM   = counts * FSR / 2^16
Ipin = IM - Ioff
Ired = nominal_Ioff - IM = -Ipin + delta_Ioff
```

由此得到以下软件与实验门禁：

1. 标定点和未知样品必须使用相同的 FSR 与 offset；中途改量程或 offset 后需要重做标定。
2. `sat != 0` 的样本无效，不能进入稳态拟合或浓度预测。
3. 除电流外，还要同时检查实际 `WE-RE` 电位、CE 裕量、VDD 与 AFE 状态。
4. offset 误差主要表现为加性直流偏差；FSR 决定量化步长和动态范围。
5. 氧化方向电流应留在当前配置允许范围内，并保留足够裕量，避免把接近轨限的数据用于标定。

理论框架给出的当前建议配置为：

```text
SET fsr=5 off=1 conv=auto period=0 e=200 vwe=1200 idle=2 sysper=3 cellv=1 ioc=0
```

该配置对应 2 uA FSR、10% offset、`E = +200 mV`、`V_WE = 1200 mV`。
它是当前实验基线，不应由 App 外壳另行覆盖。正式使用前仍应在“硬件 DEBUG”页
确认设备回报的 `CFG_APPLIED/CFG_DERIVED`、电位与饱和状态。

2026-08-12 正式标定的固化方法为 `+0.2 V / 180 s`，上位机输出
`10 Hz / 1800 点`，标定值取最后 `20 s` 的有效点。完整的机器可读配置、
编译头文件和方法说明归档在 `protocols/it_200mV_180s/`。

## 使用方式

```bash
make install
make app
open "dist/SensUs Workstation.app"
```

App 启动后显示完整工作站。“硬件 DEBUG”页继续使用该分支的运行时
配置和诊断流程；“实时测量”页提供 I-T/CV 方法条件、标定与自动保存。
悬浮窗用于查看实时 I-T 数据并开始或停止正式测量；它不另行改写
硬件配置。

## 已知边界

- 当前 OCP 功能仍按 DEBUG 分支中的已知限制处理，不把它作为正式电位依据。
- `idle=disconnect` 且静置时间为零时可能把切换瞬态写入正式数据。需要静置稳定时，
  应在实验协议中明确保留静置时间。
- DEBUG 页允许在 GUI 条件尚未“应用”时运行，目的是观察硬件真实状态；正式测量、
  标定和预测仍保留原有门禁，防止把未确认条件的数据写入工作目录。
