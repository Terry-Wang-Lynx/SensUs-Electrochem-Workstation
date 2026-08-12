# pa_converter_v51 —— V5.1 板定义 + USB 烧录(MCUboot serial recovery)

> 建立 2026-08-12。**状态:构建已通过并核实,硬件行为未实测**(见 §4 的诚实标注)。

## 0. 先回答那个问题:能不能用 USB 烧空片?

**不能。** nRF52 系列**芯片里没有出厂的 USB/串口 bootloader**(实测:V5.1 首板 Flash 头全 `0xFFFFFFFF`)。
⇒ **首烧必须 SWD**;SWD 烧一次 MCUboot 之后,才能纯 USB 烧 app。

> 对照设计记录:`hardware/ver5.1/README.md` 原本就写「用户只要求 USB 传数据;**不要求**经 USB 烧录/调试」
> ⇒ 本目录是**加功能**,不是补缺陷。

## 1. 为什么选「上电等窗口」而不是按键触发

V5.1 上除 **USB-C** 与 **CN1**(VTref/SWDIO/SWCLK/nRESET/GND)之外**没有任何物理输入**
—— 2026-08-12 逐器件核过全部 26 个件:无按键、无测试点、无引出 GPIO。

⇒ `BOOT_SERIAL_ENTRANCE_GPIO` 这类方案不可用。选定:

| 配置 | 作用 |
|---|---|
| `BOOT_SERIAL_WAIT_FOR_DFU=y` + `TIMEOUT=5000` | 每次上电先等 **5 s** 接受 USB 上传,超时才跑 app |
| `BOOT_SERIAL_NO_APPLICATION=y` | 🔴 **没有可引导 app 就永久停在 recovery** —— 这是本板唯一的"软砖纯 USB 出路" |

硬出路永远是 CN1 的 SWD。

**取值来源**:逐条抄自 MCUboot 自带的 `boot/zephyr/boards/ctcc_nrf52840.conf`,
符号存在性在本地 NCS v3.4.0 树的 `Kconfig.serial_recovery` 里 grep 核实过,不是凭记忆写。

**为什么不用 `BOOT_USB_DFU_WAIT`(dfu-util 那条)**:① 它在 MCUboot Kconfig 里挂着
`imply DEPRECATION_TEST`(旧 USB 栈将弃用);② NCS 官方文档与 nrf_desktop 主推 serial recovery;
③ V5.1 数据通路(Phase 2)本来就是 USB CDC ⇒ 主机侧只需一种 USB 类。行为上两者一致。

## 2. 构建

```sh
export ZEPHYR_SDK_INSTALL_DIR=$HOME/zephyr-sdk-1.0.1
APP=<repo>/software/firmware
cd ~/ncs && ~/ncs/.venv/bin/west build -b pa_converter_v51 -d /tmp/build_v51 "$APP" -- \
    -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DBOARD_ROOT="$APP" -DDTS_ROOT="$APP"
```

🔴 **三个必须记住的点:**

1. **`-DBOARD_ROOT` / `-DDTS_ROOT` 必须在命令行传。** sysbuild 是顶层 CMake,
   app 的 `CMakeLists.txt` 里那两句 `list(APPEND BOARD_ROOT ...)` **还没执行**
   ⇒ 不传会报 `No board named 'pa_converter_v51' found`。
   ⚠️ V4 的非 sysbuild 构建**不需要**这两个,所以这个坑只在 V5.1 出现。
2. **`SB_CONFIG_BOOTLOADER_MCUBOOT` 刻意只走命令行,没写进 `sysbuild.conf`** ——
   `sysbuild.conf` 对所有板生效,会把**正在出测量数据的 V4** 也套上 MCUboot。别加。
3. **`west` 在 `~/ncs/.venv/bin/west`**,不在项目 `.venv` 里(`west.yml` 的注释已过时)。

### 实测用量(2026-08-12)

| 镜像 | Flash | 占比 | RAM |
|---|---|---|---|
| app | 77 624 B / 188 080 B | 41.3 % | 11.4 % |
| MCUboot(带 USB CDC) | 60 936 B / 96 KB | 62.0 % | 22.9 % |

⚠️ 初版给 MCUboot 64 KB ⇒ **92.98% 满**,一长就溢出,已提到 96 KB。
**改分区表后必须重新构建并看 `Memory region` 那一行。**

## 3. 分区布局(512 KB)

```
boot     0x00000000 +0x18000 ( 96KB)   MCUboot
slot0    0x00018000 +0x30000 (192KB)   运行镜像
slot1    0x00048000 +0x30000 (192KB)   DFU 上传目标(serial recovery 只写 slot1)
storage  0x00078000 + 0x8000 ( 32KB)   settings
```

- 算法:`CONFIG_BOOT_SWAP_USING_MOVE=y`(由 `BOOT_PREFER_SWAP_MOVE` 派生,已从 `.config` 回读确认)
- ⚠️ 实测本配置下**没有**生成 `partitions.yml` / `pm_config.h` ⇒ **Partition Manager 未接管,
  DTS 里这张表就是最终布局**。若将来 NCS 版本变化导致 PM 接管,先确认哪套在生效,别盲改数字。

## 4. 🟢 验证状态:**端到端已在硬件上跑通**(2026-08-12)

| 项 | 状态 |
|---|---|
| 构建通过、两镜像都装得下 | ✅ |
| serial recovery 配置在 `.config` 里生效(非只写在片段) | ✅ 回读确认 |
| `zephyr,uart-mcumgr = &cdc_acm_uart0` 在 MCUboot 的 DTS 里解析 | ✅ 回读确认 |
| swap-using-move 生效 | ✅ 回读确认 |
| MCUboot 运行、HFXO 起振、VBUS 检出、USBD 使能 | ✅ 见下 |
| **无 app 时永久停在 recovery**(`BOOT_SERIAL_NO_APPLICATION`) | ✅ 端口数分钟后仍在 |
| **USB 枚举 + SMP 握手** | ✅ `/dev/cu.usbmodem1101`,产品串 `pA_Converter V5_1 MCUBOOT` |
| **经 USB 上传 78 KB 并启动** | ✅ `Upgrade complete.`,15.2 kB/s |
| **app 运行 + 调试口存活** | ✅ `PC=0x24F2E`(≥slot0),`unsecured` 计数 0 |

芯片侧实测寄存器(诊断时很有用):

```
CLOCK.HFCLKSTAT    (0x4000040C) = 0x00010001   SRC=xtal, STATE=running
POWER.USBREGSTATUS (0x40000438) = 0x00000003   VBUSDETECT=1, OUTPUTRDY=1
USBD.ENABLE        (0x40027500) = 0x00000001
USBD.USBPULLUP     (0x40027504) = 0x00000001   已在总线上呈现自己
USBD.FRAMECNTR     (0x40027520) = 递增          ← 主机在发 SOF ⇒ 数据线通
```

### 🔴 坑 A:macOS 的「允许配件连接」弹窗会伪装成"数据线没通"

第一次插上时 macOS 弹窗问是否信任新 USB 设备。**在点允许之前**,现象是:

- 没有任何 `/dev/cu.usbmodem*` 新增
- `USBD.FRAMECNTR` **完全冻结**、`USBD.EVENTS_USBRESET = 0`
- 而芯片侧一切正常(`USBPULLUP=1`)

⇒ 这套现象与「D+/D− 虚焊」**完全同形**,本次据此错误地判过一次"数据线没通"。
**排查顺序:先确认系统层面允许了这个配件,再去怀疑焊接。**
(判据:`FRAMECNTR` 递增 = 主机在发 SOF = 数据线电气通。)

### 🔴 坑 B:build code B 上,烧 app 之前必须先编 `UICR.APPROTECT = 0x5A`

否则 app 一跑就锁 AP,下一次 `connect` 时 **J-Link 会自动 mass erase 来"解锁"**,
连 UICR 一起擦掉(`REGOUT0` 随之丢失,VDD 退回 1.8 V)。
本次实际踩过一次,全过程与源码级证据见
`<主仓>/docs/troubleshooting/nrf52-failed-to-power-up-dap.md` §0.6。

## 5. 首烧(SWD bootstrap,一次性)—— 两个 UICR 前置条件

🔴 **烧任何东西之前,先把两个 UICR 值编好**(缺一个都会自毁,见 §4 坑 B):

```
w4 4001E504, 1          # NVMC.CONFIG = WEN
w4 10001208, 0000005A   # UICR.APPROTECT = HwDisabled  ← 缺它:app 一跑就被 J-Link mass erase
w4 10001304, 00000005   # UICR.REGOUT0  = 3.3V         ← 缺它:VDD=1.8V,SWD 电平对不上连不上
w4 4001E504, 0
r                        # 复位后生效;验 VTref 应从 1.74V 变成 ~3.17V
```

V5.1 首板已于 2026-08-12 编好两者。**换板/换芯片后必须重做**(UICR 是每颗芯片的)。

**建议只 SWD 烧 MCUboot,app 走 USB** —— 这样能顺便验证 USB 通路:

```
JL=/Applications/STM32CubeIDE.app/.../tools/bin/JLinkExe      # 🔴 必须 V8.80,不能 V9.46
si SWD / speed 1000 / device nRF52833_xxAA / connect
loadfile /tmp/build_v51/mcuboot/zephyr/zephyr.hex
r / g
```

🔴🔴 **绝对不要用 `erase`** —— JLinkExe 的 chip erase 在 nRF52 上**连 UICR 一起擦**,
上面两个值立刻丢失、SWD 当场连不上。只用 `loadfile`(它按需擦对应扇区)。

⚠️ 若要 SWD 直烧 app,必须用 **`.signed.hex`**(带 MCUboot 镜像头);
烧 `zephyr.hex` 会被 MCUboot 判为无效镜像。

## 6. 之后的 USB 烧录(✅ 已实测跑通)

主机侧用 **`smpmgr`**(纯 Python)。本机装在临时 venv 里验证过,**尚未加进项目 `.venv`**:

```sh
python3 -m venv /tmp/smpvenv && /tmp/smpvenv/bin/pip install smpmgr
```

```sh
# 1) 认端口(板子的口只在 MCUboot 窗口内、或 app 起了 USB CDC 之后才出现)
ls /dev/cu.usbmodem*
#    ⚠️ /dev/cu.usbmodem0000297345691 是 **J-Link 探头自己的** CDC(序列号 29734569),不是板子
#    板子会是类似 /dev/cu.usbmodem1101,产品串 "pA_Converter V5_1 MCUBOOT"

# 2) 握手(可选,确认在 recovery 里)
/tmp/smpvenv/bin/smpmgr --port /dev/cu.usbmodem1101 --timeout 5 image state-read
#    slot 为空时回 "No images on device!" —— 正常

# 3) 上传 + 标记启动 + 复位,一条命令
/tmp/smpvenv/bin/smpmgr --port /dev/cu.usbmodem1101 --timeout 10 \
    upgrade /tmp/build_v51/firmware/zephyr/zephyr.signed.bin
#    实测:78 KB / 15.2 kB/s / "Upgrade complete."
```

⚠️ 那条 `Error reading MCUMgr parameters ... ENOTSUP` 警告**无害** ——
MCUboot 的精简 SMP 不实现可选的 "MCUMgr parameters" 命令,忽略即可。

**时间窗**:有可引导 app 时只有上电后 **5 s**;slot0 为空时(`BOOT_SERIAL_NO_APPLICATION=y`)
**永久等待**,所以"软砖"总能纯 USB 救回来。

### ✅ 已在**完全脱离调试器**的条件下验证(2026-08-12)

SWD 探头**从板子上拔掉**后重跑一遍:

```
[捕获] /dev/cu.usbmodem1101 —— 立刻上传
100.0% • 78 kB • 15.0 kB/s
Waiting for response to ResetWrite... OK
Upgrade complete.
[t~0s] 板端口已消失 ⇒ app 已接管运行
```

🔴 **「端口消失」本身就是镜像通过校验的证据** —— 因为 `BOOT_SERIAL_NO_APPLICATION=y`:
若上传的镜像无效,MCUboot 会**永久**留在 recovery、端口不会消失。
(脱离探头后没法用 SWD 读 PC,这条推断替代了直接观测。)

### 🔴 现阶段的真实使用限制:每次烧录都要**手动拔插一次 USB-C**

当前 app 是 V4 那套 RTT 固件,**不带 USB CDC 也不带 SMP server** ⇒ 平时板端**没有** USB 口
⇒ 进 DFU 窗口的唯一办法是**物理断电重上电**。
(主机侧无法远程复位:板子唯一的电源就是那根 USB-C,macOS 也不能单口断 VBUS。)

自动化办法:挂一个轮询器,端口一出现就立刻上传,这样不用掐那 5 秒 ——
本次用的脚本见 `<主仓>` 的会话记录,核心就是
`for ...; do p=$(ls /dev/cu.usbmodem* | grep -v <探头序列号>); [ -n "$p" ] && break; sleep 0.2; done`
然后立刻 `smpmgr --port "$p" upgrade ...`。

**要做到"零接触烧录",需要 Phase 2 给 app 加上其一:**
1. **USB CDC + SMP server** ⇒ 之后可 `smpmgr os reset` 软复位进窗口(**推荐** ——
   与 V5.1 的数据通路本来就要走 USB CDC 是同一件事,一并做最省事);
2. `BOOT_SERIAL_BOOT_MODE` + retention(`gpregret`)⇒ app 收命令写标记再软复位,
   MCUboot 直接停在 recovery,连 5 s 窗口都不用等。

## 7. 两条债

1. 🔴 **签名用的是 MCUboot 默认开发密钥** `~/ncs/bootloader/mcuboot/root-rsa-2048.pem`
   —— **这是公开密钥**,bring-up 可用,**产品态必须换自己的**(`CONFIG_BOOT_SIGNATURE_KEY_FILE`)。
2. **数据通路仍是 RTT**,本轮**刻意未动** —— 迁到 USB CDC 会牵动主机侧
   `collect.py` / `gui_server.py`,那是 Phase 2。本轮只做「USB 能烧录」这一件事。

## 8. 与 V4.0 板的差异

| 项 | V4.0 | V5.1 |
|---|---|---|
| SPI 引脚(SCLK/MOSI/MISO/CSN) | P0.05 / P1.09 / P0.04 / P0.11 | **完全相同**(PCB 实解核对) |
| 供电 | CR2032 直供 VDD(Normal 模式) | **VDDH 高压模式**,VDD 由片内 REG0 出(须烧 REGOUT0) |
| `CONFIG_SERIAL` | `n` | **`y`**(USB CDC ACM 是 serial 设备) |
| USB | D+/D− 悬空 | 已接 J1 A6/A7,`&usbd` 开启 + `cdc_acm_uart0` |
| 射频 | IPEX u.FL | **ANT 悬空 ⇒ 无射频**,不要开 BT |
| `VSS_PA` | 🔴 悬空(缺陷) | 已接地(修好了) |
| DEC5 820 pF | 🔴 需要却无焊盘(build code A) | 🔴 有却不需要(build code **B**) |
| MCUboot | 无(整片给 app) | 有(双槽 + USB serial recovery) |
| 不变的约束 | DCC 悬空 ⇒ 严禁 DCDC;无 LFXO ⇒ LFRC;INTB 悬空 ⇒ 轮询 | **同样全部适用** |
