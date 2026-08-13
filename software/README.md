# software/ — V4.0/V5.1 共用电化学固件与上位机

> **本目录定位**:V4.0/V5.1 共用的 MAX30131 电化学固件与上位机收数/分析栈。V4.0 使用 RTT，V5.1 使用两个 USB CDC（SMP + DATA）；电化学状态机与行协议不分叉。**本文件是 software/ 的唯一入口**,新增/移动文件必须同步更新本文件。
>
> **建立**:2026-07-27 ｜ 更新:2026-07-31
> **阶段**:固件最小闭环**已编译通过**,待首烧 ｜ 板已回板 · SWD 已连通
> **硬件真值来源**:`docs/ver4.0/05-IC应用设计/U1-MAX30131-AFE应用.md`(寄存器级,经 critic)
> ＋ `docs/ver4.0/02-原理图设计/MAX30131-nRF52833-引脚级连接.md`
> ＋ 🔴 `docs/ver4.0/07-可观测性与调试/AFE-v4-调试口真值与bring-up流程.md`(**引脚映射与 bring-up 以此为准**)

> 🟢 **2026-07-31 状态:SWD 已连通,可以开始写 app 层了。**
> 芯片身份已确认(`INFO.PART = 0x00052833`,512KB/128KB,空片,APPROTECT 未加锁)。
> 🔴 **必须用 J-Link V8.80,不能用 V9.46** —— 根因是 V9.x 的 DLL 丢了对克隆固件的
> legacy 回退(`Old FW that does not support reading DPIDR via DAP jobs`),
> **不是探头坏了**(07-30 曾误判为"必须换探头",已更正)。
> 全部真值 + 命令见 `docs/ver4.0/07-可观测性与调试/AFE-v4-调试口真值与bring-up流程.md` §0,
> 根因与方法学见 `docs/troubleshooting/jlink-v9克隆-swd-turnaround不松线.md`。
>
> 🔴 两处仍需处置(见 §5):**`INTB` 悬空** → app 层改轮询;
> **`DEC5` 的 820pF 缺件**(实物是 build code **A**,需要它;PCB 上连焊盘都没留)→ 影响 BLE。

## 1c. 10 Hz i-t 标定与浓度预测软件

`pa_host.it_tool` 是面向当前 180 s 电化学检测的新工作流。MAX30131 的
`SENS_PERIOD` 最短约 124 ms，硬件原生约 8.06 Hz，不能产生 10 个彼此独立的
硬件样本/秒。固件因此用 AUTO 连续转换采集约 1452 个带时间戳的原生样本；上位机
按时间戳插值为严格 10 Hz、1800 点，并在输出中保留 `source_rate_hz` 和无效/饱和
标记。每次检测最终 20 s 的有效样本均值作为一个标定/预测电流点，饱和段不会被
静默纳入拟合。

```bash
# 1) 收取已启动的 10 Hz 180 s run
PYTHONPATH=software/host .venv/bin/python3 -m pa_host.it_tool measure \
  --socket 127.0.0.1:19021 --out run.csv --raw-log run.rtt.log
# 也可以把 --socket 换成 --start-jlink。默认发送 START 命令触发新一轮，
# 不再复位 MCU/AFE，测量间隙保持恒电位。

# 2) 将原生约 8 Hz 数据重采样为 10 Hz/1800 点
PYTHONPATH=software/host .venv/bin/python3 -m pa_host.it_tool resample \
  run.csv --out run_10hz.csv

# 3) 从重采样文件提取最后 20 s 稳态电流并出图
PYTHONPATH=software/host .venv/bin/python3 -m pa_host.it_tool summarize-run \
  run_10hz.csv --summary run.summary.json --plot run.png

# 4) points.csv 必须包含 concentration_um,current_nA 两列
PYTHONPATH=software/host .venv/bin/python3 -m pa_host.it_tool calibrate \
  points.csv --model ldopa_calibration.json --plot calibration.png

# 5) 用未知样品 run 的最后 20 s 预测浓度
PYTHONPATH=software/host .venv/bin/python3 -m pa_host.it_tool predict \
  --model ldopa_calibration.json --run unknown_10hz.csv
```

`calibrate` 默认拟合一次线性曲线 `current_nA = f(concentration_um)`，也支持
`--degree 2`；`predict` 对线性曲线直接反解，对多项式选择标定区间内的实根。
`software/host/tests/test_it.py` 覆盖 CSV 读取、最后 20 s、模型 JSON 往返和反解。

### 图形化工作站

双击仓库根目录的 `Launch_Electrochem_Workstation.command` 即可启动本地工作站。
界面提供逐点实时 IT 曲线、连续更新的当前电流值、末 20 秒汇总、候选标定点管理、
线性/二次拟合、浓度预测，
以及按固定起始间隔运行的连续自动测量。用户指定的文件夹是一套实验的独立工作
目录；标定、稳定化和测试的每轮数据都自动保存到该目录，并写入统一索引。主数据
文件按 `样品名称-已知浓度.csv` 命名，重复名称追加 `-r2` 等序号避免覆盖；同时保留
原生点 `-raw.csv`、汇总 JSON 和曲线 PNG。采集期间每个原生点会直接追加写入工作
目录中的 `-raw.csv`，中途停止也不会丢掉此前已经收到的点。
固件收到 RTT `START` 命令后发送 `IT_START`，达到目标样本数后发送机器可读的
`IT_DONE` 标记；采集器收到后立即收尾，不再等待额外的固定超时。后续测量不复位
MCU/AFE，测量间隙保持配置电位。

标定测量只追加候选点，不会自动改写测试曲线。标定页会按采集时间列出全部候选点，
可选择一个连续范围或逐点勾选；图中只显示选中的点，勾选状态、浓度或电流编辑会
立即更新图形。点击生成后，当前编辑内容才会保存并锁定为测试曲线。工作目录中的
`calibration-points.csv` 保存全部候选点，`calibration-selection.json` 保存本次选点，
`calibration-model.json` 保存锁定模型。后续新增候选点、稳定化 IT 或普通测试均不改变
该模型；测试汇总会记录实际使用的模型和选点 ID，以便追溯。切换工作目录会加载该
目录自己的候选点和模型，不与其他实验互相影响。

稳定化阶段的已完成 IT 会出现在“过渡期漂移校正”中。用户选择起止记录后，软件用
`bias = 结束稳态电流 - 起始稳态电流` 计算电流偏移，同时报告全部所选记录的线性
漂移斜率。只有显式启用时，bias 才会加到测试曲线；预测时等价地从实测电流中减去
bias。配置保存在工作目录的 `calibration-drift.json`，测试汇总同时记录该配置。

自动测量支持“收集候选标定点”“稳定化 IT（不改曲线）”和“测试并自动预测”三种
任务；可同时限制运行次数和阶段持续时间。例如，锁定标定曲线后可运行 60 分钟的
稳定化 IT，再使用同一条锁定曲线测试。自动测量不会重叠；由于单次检测需要
测量时长、阶跃前保持时间外加 10 秒保护余量，界面会据此限制最短自动测量间隔。
IT 条件可设置恒定测试电位（-0.4 至 +0.4 V）、时长（10 至 3600 s）、
输出采样率（0.5 至 10 Hz）、
末段拟合窗口和 50 nA 至 2 µA 量程。点击“应用条件并烧录硬件”会生成受约束的
固件配置、增量编译并烧录；MAX30131 原生上限约 8.06 Hz，高于该值的输出点由
主机按硬件时间戳重采样。固件会在采样前施加并持续保持该测试电位，在每次写
DACA/DACB 后回读，并在采集中每秒审计 DAC、路由、参考源和系统控制寄存器；连续
两次回读不一致时立即停止本轮，避免静默使用错误电位。内部运行文件保存在
`measurements/gui_runs/`；面向实验的副本和模型保存在界面指定的工作目录。

也可从终端启动：

```bash
PYTHONPATH=software/host .venv/bin/python3 -m pa_host.gui_server --open-browser
```

## V5.1 构建与 USB DATA 采集

V5.1 分区为 MCUboot `0x00000–0x17fff`、slot0 `0x18000–0x45fff`、slot1 `0x46000–0x73fff`和 storage `0x74000–0x7ffff`。应用启动前只读审计 `UICR.APPROTECT=0x5a`、`UICR.REGOUT0=5` 与 VDDH 供电状态，不写 UICR；任一项不符都不启动 USB/AFE。

```bash
# NCS 3.4.0；产物在 build/firmware 和 build/mcuboot
source ~/ncs/.venv/bin/activate
source ~/ncs/zephyr/zephyr-env.sh
west build -p always -b pa_converter_v51 -d software/firmware/build \
  software/firmware -- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y \
  -DBOARD_ROOT=$PWD/software/firmware -DDTS_ROOT=$PWD/software/firmware

# DATA 口上的行协议、START/STOP/配置命令与 V4 RTT 逐字段一致
PYTHONPATH=software/host .venv/bin/python3 -m pa_host.it_tool measure \
  --serial /dev/cu.usbmodemDATA --out run.csv --raw-log run.usb.log
```

V5.1 串口采集器打开 DATA 后会先丢弃一个可能从中间开始的旧行，再自动发送只读 `GET`/`STATUS`，最后才发 `START`。这保证每个采集文件自带 `CFG_APPLIED`/`CFG_DERIVED`/`CFG_CONFIRMED` 和测量前 `STATUS1` 证据，也避免旧的半行与 `IT_START` 拼接后引起重复 `START`。

MCUboot 和 app 的 USB 上传仍使用交接资料里的公开开发密钥，只适合 bring-up；产品发布前必须换成私有签名密钥并同步重烧 MCUboot。V5.1 现阶段还使用 Zephyr 默认测试 VID/PID，也不是产品配置。

## 1. 现在能跑什么(不需要硬件)

```bash
# 固件纯逻辑层单测(139 项断言)
cd software/firmware/tests && make test && make asan

# 上位机分析栈单测(16 项)—— 需要 numpy,用仓库根的 .venv
cd software/host && ../../.venv/bin/python3 tests/test_analyze.py

# 端到端 A:造合成数据 → 分析
../../.venv/bin/python3 -m pa_host.synth /tmp/run.csv --hours 4 --sigma 0.42 --drift 1.8
../../.venv/bin/python3 -m pa_host.analyze /tmp/run.csv --fsr-pa 50000 --plot /tmp/run.png

# 端到端 B:模拟 RTT 日志 → collect 落盘 → analyze(验收数链路本身)
python3 -m pa_host.collect --tail /tmp/fake_rtt.log --out /tmp/e2e.csv --idle-timeout 3
../../.venv/bin/python3 -m pa_host.analyze /tmp/e2e.csv --dev-clock --fsr-pa 50000
```

⚠️ RTT 采集路径仍只需标准库；V5.1 USB CDC 路径额外需要 `pyserial>=3.5`。numpy/matplotlib 只由分析和绘图流程使用。

## 1b. 回板后怎么烧 + 怎么取数

🔴 **必须用 J-Link V8.80**(V9.46 连不上,见顶部状态块):

```bash
JL880="/Applications/STM32CubeIDE.app/Contents/Eclipse/plugins/com.st.stm32cube.ide.mcu.externaltools.jlink.macos64_2.5.100.202509120932/tools/bin"

# 0) 先确认探头还在 USB 上(这支克隆板会掉线,只能物理拔插恢复)
ioreg -p IOUSB -l -w 0 | grep -A3 -i SEGGER | grep -E 'Product Name|Serial'

# 1) 烧(west build 产出 build/zephyr/zephyr.hex)
cat > /tmp/f.jlink <<'EOF'
si SWD
speed 4000
device nRF52833_xxAA
connect
r
h
loadfile build/zephyr/zephyr.hex
r
g
q
EOF
"$JL880/JLinkExe" -NoGui 1 -CommandFile /tmp/f.jlink

# 2) 取数(RTT → CSV → 指标)
cd software/host
python3 -m pa_host.collect --start-rtt --out run.csv --probe-serial 29734569
../../.venv/bin/python3 -m pa_host.analyze run.csv --fsr-pa 50000 --plot run.png
```

⚠️ **别用 `pkill` 杀 J-Link 进程** —— 会把这支探头打掉线。`collect.py` 用的是
`Popen.terminate()`,不是 pkill。

## 2. 目录与职责

```
software/
├── firmware/                     ← 同时是 Zephyr 应用根
│   ├── west.yml                  参考用清单;**实际走 freestanding**(工作区 ~/ncs)
│   ├── CMakeLists.txt            把 lib/max30131 直接编进固件(不复制源码)
│   ├── prj.conf                  RTT/SPI/看门狗;🔴 CONFIG_BT=n(刻意不开 BLE)
│   ├── boards/senseus/pa_converter_v40/  ★ 自定义板(hwmv2:boards/<vendor>/<board>/)
│   │   ├── pa_converter_v40.dts        SPI 引脚(🔴MOSI=P1.09 在 port 1)、无 UART、wdt0、
│   │   │                               🔴&reg1=LDO(DC/DC 电感未贴,严禁 DCDC 模式)
│   │   ├── pa_converter_v40_defconfig  LFRC(LFXO 已砍)+ MPU 栈保护
│   │   └── board.yml / Kconfig / Kconfig.defconfig / Kconfig.<board> / board.cmake / *.yaml
│   ├── dts/bindings/                 自建 binding + vendor 前缀(hwmv2 要求)
│   │   ├── spi/senseus,max30131.yaml   最小 SPI 从设备 binding(仅为出 DT 宏)
│   │   └── vendor-prefixes.txt         senseus(Zephyr 主表里没有)
│   ├── src/
│   │   ├── main.c                时序编排 + 轮询取数 + RTT 行输出
│   │   ├── max30131_spi.c/.h     传输层(🔴帧=[地址][命令 R/W@bit7][数据])
│   │   └── board_guards.c/.h     DCDCEN 断言 + POFCON 2.0V + 看门狗
│   ├── lib/max30131/          ★ 纯逻辑层(零硬件依赖,可在 Mac 上单测)
│   │   ├── max30131_regs.h      寄存器地址/位域/FIFO 格式/SPI 帧(datasheet 真值)
│   │   ├── max30131.h/.c        寄存器编码、FIFO 解包、counts↔电流(pA 与 **fA**)、
│   │   │                        DAC/极化、共模校核、时序表、手动增益校准反算
│   └── tests/                   minitest.h + test_max30131.c + Makefile(139 断言)
└── host/
    ├── pa_host/
    │   ├── record.py          ★ 固件 RTT 行协议 ↔ CSV 的单一真源(纯标准库)
    │   ├── collect.py           A 段:RTT/USB CDC → CSV,边收边查完整性
    │   ├── analyze.py           B 段:σ/3σ/PSD/Allan/漂移/ER·NFR + 出图(需 numpy)
    │   └── synth.py             合成数据(回板前跑通全链;禁止用于对外指标)
    └── tests/test_analyze.py    用「已知答案」反验算法
```

🔴 **`lib/max30131/` 只有一份源码**,既被 `CMakeLists.txt` 编进固件、又被 `tests/`
用 clang 在 Mac 上直编直跑。**不要复制到 src/** —— 那会让单测验证的和固件跑的分叉。

**分层理由**:写这层时板子还没回来、也没有 DK,所以把「会算错且回板当天才暴露」的东西
全部压进 `lib/max30131/` 的纯函数里,用 clang 直编直跑钉死。
传输层(Zephyr SPI/GPIO)与 app 层刻意做薄。
刻意**不用 native_sim**——它不是 macOS 的官方支持目标,不值得赌。
**事后验证了这个取舍是对的**:首次 `west build` 编译器只报 2 个 C 层错误,
而"逐个核对 lib 符号"这套非编译器手段抓出了 4 处错名 + 1 个位布局真 bug(见 §5.2e)。

## 3. 工作点(与 05 文档一致,固件里以常量出现)

- Sn_FSR = **50nA 档**(LSB 763 fA)｜ offset = **固定 10nA**(覆盖 5nA 信号峰)
- 换算 `I_还原 = offset − counts×FSR/2¹⁶`(counts 增大 ⇒ 还原电流减小)
- 基准 1.536V 内部 ｜ V_WE=DACA=0.4V ｜ E=−0.2V ⇒ V_RE=DACB=0.6V(code 0x640)
- CONV_TIME=0x4(1.882s / 16 位)｜ SENS_PERIOD=0x5(3.757s)⇒ **≈0.27 SPS**
- 关键寄存器:`0x20=0xC5 0x21=0x90 0x22=0x08 0x23=0x04 0x24=0x09 0x68=0x01
  0x80=0x05 0x83=0x03 0x05=0x80 0x10=0x0C`(全部在单测里断言)

## 4. 🔴 落盘的坑(写固件前必读)

本次照 datasheet 逐条核实时,发现三处「凭直觉或凭现有文档写就会错」的地方:

1. **FIFO_A_FULL 是「剩余空位」不是「样本数」**
   - 现象:A_FULL 置位时 FIFO 内样本数 = `256 − FIFO_A_FULL`。
   - 根因:`05/U1` §转换/FIFO/中断 写「watermark 取小值,如 16,~1 分钟一批」——
     照字面写 16 会变成 **240 个样本一批 ≈ 15 分钟**,正好反了。
   - 解决:用 `max30131_fifo_a_full_from_batch(16)` → 240(0xF0)。
   - 验证:`test_fifo_a_full_is_free_space_not_sample_count`(含反面断言)。
   - **待回灌**:`docs/ver4.0/05-IC应用设计/U1-MAX30131-AFE应用.md` 那句需改。

2. **SPI 帧是「地址字节 + 独立命令字节 + 数据」,R/W 在命令字节 bit7**
   - 现象:常见约定 `(addr<<1)|R/W` 在这颗器件上是错的。
   - 根因:datasheet Fig.24/25/26 的 SDI 序列是 `A7..A0 | R/W X.. | D7..D0`。
   - 解决:`MAX30131_SPI_CMD_WRITE=0x00` / `..._READ=0x80`;SPI mode 0。
   - 验证:回板 bring-up 读 `0xFF PART_ID` 即证(纯逻辑层无法自证)。

3. **DAC 是「MSB 字节 + EN/LSB 字节」的非直觉布局**
   - 现象:按 `hi=code>>8, EN@bit7` 写会把极化电位设成完全错的值(恒电位仪失效)。
   - 根因:`0x69=CODE[11:4]`,`0x6A=CODE[3:0]<<4 | 保留 | EN(bit0)`。
   - 解决:`max30131_enc_dac()` / `max30131_dec_dac_code()`。
   - 验证:`test_dac_code_and_byte_layout`(断言 0x640→msb=0x64/lsb=0x01)。

另有一处**行协议**自查修正:电流字段用 **整数 fA** 而非整数 pA——50nA 档 LSB
只有 763 fA,用 pA 会让协议本身比器件还粗,把亚 pA 噪声量化掉(端到端测试
里表现为 σ 从 0.42 虚高到 0.50 pA)。

## 5. 还没写的(下一步)+ 🔴 两处已知需返工的设计前提

### 5.1 🔴 `INTB` 悬空 → app 层必须改成轮询

PCB 实解:`unconnected-(U1-INTB-PadB3)`,MAX30131 的 INTB **没连到 nRF 任何脚**。
所以 `05/U1` 文档的「AUTO 自主 + FIFO watermark → INTB 唤醒」**在硬件上不存在**。

✅ **2026-07-31 用户拍板:接受轮询,不飞线。** app 层已按此实现
(`drain_fifo()` 定时读 `0x0C/0x0D FIFO_COUNTER1/2`)。代价:平均功耗高于原预算。
🔴 注意寄存器真名是 **`FIFO_COUNTER1/2`**,且 `[7]=DATA_COUNT[8] | [6:0]=OVF_COUNTER`
**打包在一个字节**(见 §5.2e 的位布局 bug)。
将来若飞线接了 INTB:把 `drain_fifo()` 换成中断回调即可,`FIFO_A_FULL` 已按正确语义设。

### 5.2 ✅ 探头/工具链已定案:J-Link **V8.80**,RTT 路线不用改

**2026-07-31 结案。** 用 STM32CubeIDE 自带的 V8.80 工具链,SEGGER 生态全部可用:

```bash
JL880="/Applications/STM32CubeIDE.app/Contents/Eclipse/plugins/com.st.stm32cube.ide.mcu.externaltools.jlink.macos64_2.5.100.202509120932/tools/bin"
"$JL880/JLinkExe"            # 连接/读写/烧录
"$JL880/JLinkRTTLoggerExe"   # RTT 落盘 → collect.py 直接读这个文件
"$JL880/JLinkGDBServerCLExe" # 调试
```

⇒ **`collect.py` 按 SEGGER RTT 写即可,本目录设计一行不用改。**
🔴 但 **`/usr/local/bin/JLinkExe`(V9.46)连不上**,脚本里必须写 V8.80 的绝对路径。
⚠️ V8.80 是 x86_64(Rosetta),实测正常。
⚠️ 这支克隆探头**会掉出 USB**,连接前先 `ioreg -p IOUSB | grep -i SEGGER` 自检;
**别用 `pkill` 杀 J-Link 进程**(会把它打掉线,只能物理拔插恢复)。

`nrfjprog` 本机未装 —— V8.80 的 `JLinkExe` + `JFlash` 已够用,暂不需要。
将来若买 CMSIS-DAP 探头(Pi Debug Probe / WCH-LinkE)才需要 probe-rs;
把"读一行 → `parse_line()`"的接口留干净即可,换源不改解析。

### 5.2b 🔴 `DEC5` 820pF 缺件 —— 固件侧无法规避,BLE 前必须处置

实物 `INFO.VARIANT = "AAA1"` ⇒ **build code A**,datasheet 要求 `DEC5`(pin21)挂
**820pF** 作 1.3V 内部稳压器去耦(Bxx+ 才不需要)。**PCB 上 pin21 完全悬空,无焊盘。**

- 轻载(SWD/读写/AFE 采样)实测正常
- ⚠️ 风险窗口是 **BLE TX 的 mA 级电流突变**(LDO 模式 TX 10.3mA):
  可能表现为发射时复位/掉连,且随温度与电池内阻漂移
- 固件侧**没有任何办法补偿**(这是模拟去耦,不是配置项)

⇒ **写 BLE 之前先拍板**:飞线补 820pF / 先做 BLE 压力测试后补 / 下版修。
详见 `docs/ver4.0/07-可观测性与调试/AFE-v4-调试口真值与bring-up流程.md` §2.4。

### 5.2c 背景:为什么 RTT 是唯一数据通路(仍然成立)

V4 调试口 `CN1`(MX1.25 5P)只有 SWDIO/SWCLK/nRESET/VTref/GND,**没引 UART**
⇒ 无 BLE 时 **RTT 是唯一有线数据通路**。V8.80 自带 `JLinkRTTLoggerExe`,该需求已满足。

**买正品探头仍然值得**(nRF52840-DK 最省事:正品 J-Link OB + 已知好目标 + 一路 3.3V 供电),
但性质从"阻塞项"降为"消除长期约束"——可以跟上新版工具链、也便于交叉验证。
⚠️ J-Link EDU / EDU Mini 许可**仅限非商业/教育用途**。

### 5.2d ✅ 已编译通过(2026-07-31)—— 从报错到过,修了 5 处

```
[156/156] Linking C executable zephyr/zephyr.elf
Flash  56,088 B / 512 KB = 10.7%      RAM  11,648 B / 128 KB = 8.9%
```

**构建命令**(freestanding:工作区 `~/ncs`,应用留本仓库;环境详见 `tools/SOP-手动操作.md`):

```bash
export PATH="$HOME/ncs/.venv/bin:$PATH"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr ZEPHYR_SDK_INSTALL_DIR="$HOME/zephyr-sdk-1.0.1"
cd ~/ncs
APP=/absolute/path/to/SensUs-Electrochem-Workstation/software/firmware
west build -b pa_converter_v40 -d /tmp/pabuild "$APP" -- -DBOARD_ROOT="$APP" -DDTS_ROOT="$APP"
```

🔴 **`-DBOARD_ROOT` / `-DDTS_ROOT` 必须走命令行**,不能只靠 app 的 `CMakeLists.txt`——
见下表第 1 条。

**生成的 devicetree 已核对**(最容易错的那个引脚是对的):
`psels 0x5000029` → **P1.09**(MOSI 在 port 1)· `0x4000005` → P0.05(SCK)·
`0x6000004` → P0.04(MISO)· `cs-gpios = <&gpio0 0xb 0x1>` → P0.11 低有效。

| # | 报错 | 真因与修法 |
|---|---|---|
| 1 | `No board named 'pa_converter_v40'` | **NCS 3.x 默认 sysbuild**,构建从 `zephyr/share/sysbuild` 起,app 的 `list(APPEND BOARD_ROOT)` 在解析板名时还没被读到 → 必须命令行传 |
| 2 | `undefined symbol BOARD_ENABLE_DCDC` | 🔴 **`CONFIG_BOARD_ENABLE_DCDC` 已从 Zephyr 整体移除**。我原本把"DCDCEN=0 结构化落死"押在一个**不存在的 Kconfig** 上 —— 等于没落死。改为 dts 显式 `&reg1 { regulator-initial-mode = <NRF5X_REG_MODE_LDO>; }`,比原来更硬(SoC 默认虽也是 LDO,显式声明才不会因上游默认值变化而失守) |
| 3 | `Aborting due to Kconfig warnings` | `STACK_SENTINEL` **`depends on !MPU_STACK_GUARD`**,与 `HW_STACK_PROTECTION` 互斥。nRF52833 有 MPU → 留硬件保护、删软件哨兵 |
| 4 | `too few arguments to max30131_check_period_vs_conv` | 签名是 4 参数 `(conv, period, clk_sel_40k, fsr)`。我不但少传一个,还把"快钟组"布尔塞进了 `clk_sel_40k` 位置 —— **语义也错**(分组该由函数从 fsr 推导)。已引入 `WP_CLK_40K` 常量统一口径 |
| 5 | `Delay parameter in SPI DT macros is deprecated` | `SPI_DT_SPEC_GET` 现在两参数,CS 时序改由 DT 属性给 |

`board_guards.c` **一次编过** —— `nrf_power_dcdcen_get/set`、`nrf_power_pofcon_set`、
`NRF_POWER_POFTHR_V20`、看门狗 API 全部正确,无需调整。

### 5.2e 编译器之外的验证手段(仍然有用,回归时先跑这些)

写 app 层时本机还没有 NCS,于是先用这些不依赖编译器的手段兜住 —— 事后证明**它们抓到的
错比编译器多**(编译器只报了 2 个 C 层错误,下面这套抓出 4 处错名 + 1 个位布局真 bug):
- ✅ `src/main.c` 引用的**每一个** lib 符号都逐个核过存在性(查出 4 处错名并修:
  `FIFO_DATA_COUNT`/`OVF_COUNTER` 不存在、结构体叫 `max30131_fifo_word_t`、
  极化字段是 `code_a`/`code_b`)
- ✅ 顺带查出一个**真 bug**:`FIFO_COUNTER1` 是 `[7]=DATA_COUNT[8] | [6:0]=OVF_COUNTER`
  **打包在一个字节**,按 `(c1<<8)|c2` 读会把溢出计数当数据计数高位
- ✅ lib 新增 `max30131_counts_to_reduction_fa()` + 6 项断言(含反面:证明 pA 口径
  分辨不出 1 LSB)
- ✅ 上位机链路端到端跑通(模拟 RTT 日志 → collect → analyze),16 项单测回归干净

当时**预判了 4 个修点,命中 2 个**(Kconfig hwmv1/hwmv2 骨架、`vnd,spi-device` binding 不可用);
`nrf_power` HAL 头路径预判错了(一次编过);另外**没预判到**的两个反而更关键 ——
`CONFIG_BOARD_ENABLE_DCDC` 已被 Zephyr 移除、`STACK_SENTINEL` 与 MPU 互斥。
⇒ 教训:**猜编译错误的性价比低于把符号/机制逐个核实**。实际修点全表见 §5.2d。

### 5.3 其余待写

- **装 NCS 工具链并首次 `west build`** ← 现在最大的一步(见 §5.2d 的预期修点)
- 校准系数持久化(NVM)+ 方向 A/B 切换开关(对应 05 文档 §6 待实测项)
- 开 BLE 前:先处置 `DEC5`(§5.2b)
- 接了 INTB 后:把 `drain_fifo()` 从定时轮询改成中断回调(逻辑不变,
  `FIFO_A_FULL` 已按正确语义设,不用改)

**三条焊死项已落进代码**(不再是文档叮嘱):

| 约束 | 落在哪 |
|---|---|
| `DCDCEN=0`(电感未贴,置 1 连 SWD 一起死) | `boards/.../pa_converter_v40_defconfig` 的 `CONFIG_BOARD_ENABLE_DCDC=n` + `board_guards.c` 运行时断言并强制关闭 |
| POFCON ≈2.0V + 看门狗 | `board_guards.c`(狗装不上就**拒绝进入采集**,因为失去 brownout 唯一自恢复路径) |
| LFXO 已砍 → LFRC ±500ppm | `pa_converter_v40_defconfig` 的 `CONFIG_CLOCK_CONTROL_NRF_K32SRC_RC` |

**APPROTECT 更正**:一般规则是 build code Bxx 起出厂默认 enabled、首烧前需 recover;
但**本板实测是 Axx、未加锁、无需 recover**(`UICR.APPROTECT=0xFFFFFFFF`)。
🔴 换料/换批次后必须重读 `FICR.INFO.VARIANT`(它同时决定 APPROTECT 与 DEC5 是否需要 820pF)。

## 6. 相关文档

- 寄存器/工作点真值:`docs/ver4.0/05-IC应用设计/U1-MAX30131-AFE应用.md`
- 引脚/供电/时钟/调试口:`docs/ver4.0/02-原理图设计/MAX30131-nRF52833-引脚级连接.md`
- nRF 踩坑清单:`docs/ver4.0/02-原理图设计/nRF52833-网友踩坑检查清单.md`
- 指标与验收口径(σ/3σ/漂移/ER·NFR/Allan 的定义):
  `docs/ver3.1/08-测试与表征/AFE-测试指标与验收口径.md`
