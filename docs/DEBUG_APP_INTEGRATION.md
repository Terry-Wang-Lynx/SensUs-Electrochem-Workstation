# DEBUG 分支 App 整合说明

## 目的与基线

本 App 以 `feat/afe-runtime-config-vdd-debugpage` 分支的提交
`2603bd9` 为唯一硬件与采集基线。整合的目的不是把旧版工作站逻辑混入
DEBUG 分支，而是给这套已验证的软件增加 macOS App 外壳，使其可以直接启动、
保持置顶，并在需要时切换到悬浮检测窗。

本次没有改动 DEBUG 分支中的下列核心实现：

- `software/firmware/` 下的 MAX30131 配置、运行时命令、System ADC 与 VDD 采集
- `software/host/pa_host/collect.py` 的 J-Link/RTT 采集链路
- `software/host/pa_host/gui_server.py` 的正式测量与硬件 DEBUG API
- `software/host/pa_host/gui/` 原有的完整 Web 工作站页面

新增内容只包括 macOS App 启动外壳、可选悬浮窗、App 构建入口和本说明。
旧 `main` 分支中的 CV、旧固件和旧采集实现没有合并进来。

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

App 启动后显示的完整页面就是 DEBUG 分支原有界面。“硬件 DEBUG”页继续使用
该分支的运行时配置和测试流程。工具栏中的悬浮窗用于查看实时 I-T 数据并开始或
停止正式测量；它不会修改 DEBUG 分支的固件配置逻辑。

## 已知边界

- 当前 OCP 功能仍按 DEBUG 分支中的已知限制处理，不把它作为正式电位依据。
- `idle=disconnect` 且静置时间为零时可能把切换瞬态写入正式数据。需要静置稳定时，
  应在实验协议中明确保留静置时间。
- DEBUG 页允许在 GUI 条件尚未“应用”时运行，目的是观察硬件真实状态；正式测量、
  标定和预测仍保留原有门禁，防止把未确认条件的数据写入工作目录。
