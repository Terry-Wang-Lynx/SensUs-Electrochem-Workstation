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

## 4. 验证到什么程度(诚实标注)

| 项 | 状态 |
|---|---|
| 构建通过、两镜像都装得下 | ✅ 实测 |
| serial recovery 全部配置在 `.config` 里生效(非只写在片段) | ✅ 回读确认 |
| `zephyr,uart-mcumgr = &cdc_acm_uart0` 在 MCUboot 的 DTS 里解析成功 | ✅ 回读确认 |
| swap-using-move 生效 | ✅ 回读确认 |
| **上电等 5 s / 无 app 停住 / USB 上传成功** | ❌ **未在硬件上验证** |

## 5. 首烧(SWD bootstrap,一次性)

🔴 **前置条件:`UICR.REGOUT0` 必须已烧成 3.3 V**,否则 VDD=1.8 V、SWD 电平对不上根本连不上。
V5.1 首板已于 2026-08-12 烧入(`REGOUT0=5`),全过程见
`<主仓>/docs/troubleshooting/nrf52-failed-to-power-up-dap.md` §0。

```
JL=/Applications/STM32CubeIDE.app/.../tools/bin/JLinkExe      # 🔴 必须 V8.80,不能 V9.46
si SWD / speed 1000 / device nRF52833_xxAA / connect
loadfile /tmp/build_v51/mcuboot/zephyr/zephyr.hex
loadfile /tmp/build_v51/firmware/zephyr/zephyr.signed.hex
r
```

⚠️ app 必须烧 **`.signed.hex`**(带 MCUboot 镜像头),烧 `zephyr.hex` 会被 MCUboot 判为无效镜像。

## 6. 之后的 USB 烧录

上传件:`/tmp/build_v51/dfu_application.zip`(内含 `firmware.signed.bin`)。

🔴 **主机侧 SMP 客户端本机未装**(`mcumgr` / `nrfutil` / `newtmgr` / `smpmgr` 全无)。
需要装一个;建议 **`smpmgr`**(纯 Python,可进项目 `.venv`,与现有主机栈一致)。

流程:插 USB → 板子在 5 s 窗口内枚举出一个 CDC ACM 口(macOS 上是 `/dev/cu.usbmodem*`)
→ 用 SMP 客户端上传到 slot1 → MCUboot swap 后跑新镜像。

⚠️ 分辨串口:`/dev/cu.usbmodem0000297345691` 是 **J-Link 探头自己的** CDC(序列号 29734569),
不是板子。板子的口只在 MCUboot 的 DFU 窗口内、或 app 起了 USB CDC 之后才出现。

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
