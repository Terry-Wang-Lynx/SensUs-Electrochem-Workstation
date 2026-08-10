/*
 * pA-Converter V4.0 固件最小闭环
 *   MAX30131 单芯片 AFE(SPI)+ nRF52833 + CR2032 · 数据经 RTT 上报
 *
 * 🔴 本版**不开 BLE**(用户 2026-07-31 拍板:蓝牙一时半会不用)。
 *    连带好处:`DEC5` 缺 820pF 的风险窗口正是 BLE TX 的 mA 级电流突变,
 *    不开 BLE 就绕开了它(见 docs/ver4.0/07 §2.4)。开 BLE 前必须先处置 DEC5。
 *
 * 🔴 采集方式 = **轮询**(用户拍板:接受轮询、不飞线)。
 *    因为 MAX30131 的 INTB(U1.B3)在 PCB 上悬空,中断链在硬件上不存在。
 *    代价:必须按最坏延迟定时醒,平均功耗高于原预算。
 *
 * 分层:本文件只做「时序编排 + I/O」。所有换算与寄存器编码都调 lib/max30131/
 * 的纯函数(139 项断言在 Mac 上钉死),**不在这里重算任何数值**。
 */

#include "board_guards.h"
#include "max30131_spi.h"

#include "afe_cfg.h"
#include "max30131.h"
#include "max30131_regs.h"
#if __has_include("measurement_config.h")
#include "measurement_config.h"
#else
#include "measurement_config.default.h"
#endif

#include <SEGGER_RTT.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <stdio.h>
#include <string.h>

LOG_MODULE_REGISTER(main, CONFIG_LOG_DEFAULT_LEVEL);

/* ================================================================== */
/* 工作点(与 docs/ver4.0/05-IC应用设计/U1 一致,全部在单测里断言)     */
/* ================================================================== */
/*
 * 🔴 2026-08-09(方案 C):FSR / offset 从**编译期常量**改为**运行时变量**。
 *
 * 动机 —— 打破一个死锁:原来改量程必须重编译 + 烧录 + **复位 MCU**,而复位会
 * 中断极化(DAC 回默认态,电解池失去设定电位),重新加回后必然引入一个初始
 * 瞬态。实测该瞬态峰值可超过 1 µA(r5 达 +672 nA、r6 撞 1 µA 轨 22 s),
 * 而还原侧可测上限 = offset;**电流一超过 offset,WE 就被顶出设定电位、
 * 恒电位环开环**(datasheet p41,已过 critic,见 05-IC应用设计:14)。
 * ⇒ 想用小量程拿细 LSB,就必然先经历一段开环瞬态;而想避开瞬态就得用大 offset
 *   ⇒ 参数调优无法化解,只能让"换档"不再需要复位。
 *
 * 改后流程:大 offset 起步 → 等瞬态自然衰减到远低于目标 offset →
 *           **在线切档(只写 0x23/0x24,不动 DACA/DACB,极化全程不中断)** → 采数。
 *
 * 初值仍取自 measurement_config.h ⇒ 不发 RANGE 命令时行为与改动前逐位一致。
 * 命令格式见 poll_control_command() 与 apply_range()。
 *
 * 🔴 2026-08-10(硬件 DEBUG 模式):运行时可配的范围从 FSR/offset **扩到全部**
 * ADC 侧参数 + 电位 + idle 三态 + 电位连采,统一收进 `cfg_live` 这一个结构。
 * 为什么不继续加零散的 `wp_xxx` 变量:两个旋钮管同一件事是本项目已经踩过的陷阱
 * (`WP_KEEP_CONVERTING_BETWEEN_RUNS` 与 `WP_IDLE_MODE`),而且散变量无法整体
 * 校验、无法排写序、无法一行审计。解析/派生/校验/写序全在 lib/afe_cfg(纯函数、
 * 5380 项断言),本文件只负责「执行 plan + 回读 + 确认 + 回滚」。
 */
static afe_cfg_t     cfg_live;   /* 唯一权威的生效配置 */
static afe_derived_t drv_live;   /* 它的全部派生量(审计行逐字段打印) */
/*
 * 配置纪元。每次真正写寄存器前 +1,并进入**每一个样本行**(`S ... ep=`)。
 * 🔴 为什么必须进样本行而不是靠"事件行 + 主机插值":`counts` 与 `sat` 的量纲
 *    本身随 epoch 变(sat 余量依赖 FSR/offset),不带 epoch 的 counts 无法解释;
 *    而上行 RTT 是丢包信道(NO_BLOCK_SKIP),审计行丢了主机就会静默错归。
 * ⚠️ 只有存在 `CFG_CONFIRMED ep=n` 的 epoch 才可信。
 */
static uint32_t cfg_epoch;
static bool     acquiring;    /* 采集循环是否在跑(扰动键的门禁依据) */
static bool     run_tainted;  /* 本轮被 FORCE 扰动过 ⇒ IT_DONE 带 tainted=1 */

#define WP_FSR        cfg_live.fsr
/*
 * 🔴 2026-08-01 由 SEL4(9nA)升到 SEL5(19nA)—— **不是优化,是修 bug**。
 * 依据:docs/左旋多巴标定/ 的 CHI660E 实测(E=−0.2V,与本工作点相同),
 * 稳态电流 6.07 / 7.23 / 9.15 / **12.80** nA(浓度 6.25/12.5/25/50)。
 * 而还原方向的量程上限 = offset(datasheet p41:还原电流 > offset ⇒ WEn 被顶出
 * 设定电位、**失去恒电位控制**),SEL4 的 typ 9nA / min 7nA **盖不住 12.8nA**
 * ⇒ 高浓度点会整段钳 0nA、数据作废。SEL5 typ 19nA / min 16nA,min 也有 1.25× 余量。
 * 代价:基线热漂随 offset 放大(≈offset×0.034%/°C),9nA→3.1pA/°C 变 19nA→6.5pA/°C。
 * 该权衡有单测把关:test_ldopa_calibration_currents_fit_the_range。
 */
#define WP_OFFSET_SEL cfg_live.off

/*
 * 一行命令的处理入口。定义在 idle/切档函数之后(它要用它们),这里先前向声明,
 * 让 poll_control_command() 只管拼行、不管语义。
 */
static void handle_command_line(const char *line);
static void afe_status_poll(const char *why, bool force);
static void replay_state(const char *src);
#define WP_REF        MAX30131_REF_1536MV      /* 内部 1.536V;CR2032 EOL 2.0V 只此档 */

#define WP_DEFAULT_V_WE_MV 400                 /* WE 电位 0.4V(可 SET vwe= 改) */
#define WP_V_WE_MV    cfg_live.vwe_mv
#define WP_E_MV       cfg_live.e_mv            /* E = V_WE - V_RE(测量电位) */
#define WP_STARTUP_E_MV GUI_WP_START_E_MV      /* 用户可见的阶跃起始电位 */
#define WP_PRESTEP_DURATION_MS GUI_PRESTEP_DURATION_MS
#define WP_RUN_STARTUP_DIAGNOSTIC false         /* i-t 正式测量不扫其他电位 */
/*
 * 双档增益标定开关。🔴 **保持 false —— 2026-08-09 实测它当前产不出有效结果。**
 *
 * 那次实测(FSR 1µA + offset 50%FS,重复两轮):
 *   步1 500nA 参考档 counts=4282 → offset 真值 32669 pA,而 SEL6 规格窗口是
 *       [34000, 46000] ⇒ **低于下限 3.9%**,被自身合理性闸门判作废
 *   步2 1µA 目标档 counts=2438 → FSR 真值 878177 pA(标称 1e6)⇒ **−12.1%**,
 *       也会撞 ±10% 闸门(出厂增益规格只有 ±1~2%,不该差这么多)
 *   另有一轮步1 三次转换全读到 counts=1(≈0)后放弃 —— 模式切换后的第一次
 *   转换不稳定,非确定性(重试机制存在但 3 次不够)
 * ⇒ 两条独立测量同向偏低,根因未定。可疑方向:步1 在慢钟组(500nA,
 *   CONV_TIME 0x4 = 1882ms)、步2 在快钟组(1µA,同码却是 471ms),
 *   两步的实际积分时间差 4 倍而代码只写了一次 CONV_TIME。**需查 datasheet 定案。**
 *
 * 即使修好,也**不该无条件开**:步2 反解精度受 ADC ±80LSB 限制,
 * FSR 越大越差(50nA ±0.15% / 250nA ±0.76% / 500nA ±1.53% / 1µA ±3.05% /
 * 2µA ±6.10%),交叉点约 FSR 655nA。而增益误差不被标定曲线截距吸收
 * ⇒ 仅 FSR ≤250nA 时全套才明显净赚。
 *
 * 🟢 真正无条件净赚的是**步3**(工作档 offset-only 零电流基线):它消掉
 *    offset 源容差与 ADC 固有偏移,且**不依赖步1/步2 的跨档推理**。
 *    但现在三步绑死(步1 一作废就 goto restore,步3 根本不跑),要单独用需改结构。
 * 详见 docs/troubleshooting/electrochem-workstation-烧录与rtt取数.md §12
 */
#define WP_RUN_AFE_GAIN_CALIBRATION false

/* ADC 时钟源:false = 34.952kHz(慢钟),true = 40.96kHz。可 SET clk40= 改。 */
#define WP_DEFAULT_CLK_40K false
#define WP_CLK_40K    cfg_live.clk40

/*
 * 🔬 A/B 实验(2026-08-09):`Sn_IOS_MODE`(0x22 bit3)的极性在 datasheet 里**自相矛盾**。
 *   - 寄存器图 p96:`0` = offset 仅 ADC 转换期间;`1` = **常在**。且 **Reset = 0b1**。
 *   - 正文 p33–34:置 `1` = **仅 ADC 转换期间**(与寄存器图相反)。
 *   两处都同意"默认是常在",但只有寄存器图自洽(复位值 == 它自己说的默认行为),
 *   所以此前判寄存器图为准、取 0x08。
 *
 * 为什么要做 A/B:每轮收尾会写 `CONVERT_START=(AUTO=0,CONVERT=0)` 停转换,而 p40 说
 *   **不转换时 WE 由固定 50nA 偏置、转换时才换成编程 offset** ⇒ 轮次边界上 WE 节点的
 *   抽流有一次 `(offset − 50nA)` 阶跃(500nA 档 = **450nA**)。实测每轮开头都有一次
 *   几百 nA 的**还原方向**跳变,且**无复位、无电位阶跃时同样出现**
 *   (it_20260809_182600:上一轮末 +25.88nA → 本轮首 +499.88nA 撞轨,只隔 142ms
 *   一个采样周期,seq/ms 连续 ⇒ 排除复位;固件日志 `无阶跃` + DAC 回读逐位相同 ⇒ 排除电位阶跃)。
 *   量级与这个阶跃相符。**若正文才是对的,则 IOS_MODE=1 恰好选中"仅转换期间"= 扰动最大档。**
 *
 *   A(原)= 0x08(IOS_MODE=1)   B(本次)= 0x00(IOS_MODE=0)
 *   判据:① 运行第一个样本的 counts/fa;② **无复位**轮次边界的跳变幅度。
 *   这一位同时判定 datasheet 哪一边对 —— 无论结果如何都要回灌 05 文档 §7。
 *
 * ❌ **A/B 结果:阴性(2026-08-09 实测)。** 组 B(0x00)的边界跳变照旧、照旧撞轨:
 *   A(0x08):间隔 33.8s,+25.88nA → **+499.88nA**(counts 8, sat=1),跳 +474nA
 *   B(0x00):间隔 32.1s,+97.29nA → **+482.79nA**(counts 1128, sat=1),跳 +385nA
 *   根因:我漏读了 p40 那句是**无条件**的 ——「When the ADC is not converting, S1.n is
 *   switched to VDD, S2.n is closed, and the fixed 50nA bias current is used to bias the
 *   WEn pin」。`IOS_MODE` 只管**编程 offset 在不转换时是否还在**,管不了 S1.n 的
 *   VDD/ADC 切换、也管不了那个固定 50nA 源 ⇒ 只要"停转换"发生,通路就要变一次。
 *   ⚠️ 这次 A/B 因此**也没能判定 datasheet 的极性矛盾**(我把机制假设与极性判定塞进了
 *   同一个实验,只有前者得到回答)。极性仍以寄存器图为准 ⇒ 恢复 0x08。
 */
/* IOS_MODE=1=常在(寄存器图 p96,唯一自洽);A/B 组 B 的 0x00 已阴性。可 SET ios= 改。 */
#define WP_DEFAULT_IOS true
#define WP_S1_CONFIG3 max30131_enc_s1_config3(cfg_live.ios, false)

/*
 * 🔬 A/B 实验二(2026-08-09):**轮次之间不要停转换。**
 *
 * 上一个实验把嫌疑人缩小到了"停转换"这个动作本身(而不是它的某个副作用):收尾会写
 * `CONVERT_START=(AUTO=0,CONVERT=0)`,于是两轮之间有 **~32s AFE 完全不转换**,
 * 那段时间 WE 只由 p40 的"不转换"通路(S1.n→VDD + 固定 50nA)偏置。重开转换时要恢复,
 * 观测到的两个时间尺度与此相符:
 *   - <1s 的快速恢复(B 第2轮 counts 1128→9112→13192→15328,0.5s 内爬到 +266nA)
 *     ——更像**重启后第一次转换是残缺积分**(积分不足 ⇒ counts 偏低 ⇒ 读成大还原电流),测量伪影
 *   - ~100s 的慢尾(+266nA 爬回零)——真实,疑为那段偏置通路改变留下的电化学债
 *
 * ✅ **A/B 二结果:阳性。** 不停转换后,轮次边界跳变 +474/+385nA → **−1.7nA**。
 *   而且 gap 长度已被独立排除:停转换的最短 gap(15.5s)瞬态最大(999nA 顶轨),
 *   不停转换的最长 gap(172.5s,长 11 倍)瞬态最小(~20nA)。若 gap 长度是驱动量,
 *   这张表必须是反的。⇒ **"停转换"这个动作是原因。**
 *   (该对照来自用户 2026-08-09 深夜自跑的 10 轮,不是我排的 C_run3 —— 那轮因溶液
 *    干掉被我中止,归因当时并不成立,是后来用他的数据补上的。)
 *
 * 🔻 本开关已被 WP_IDLE_MODE 取代(两个旋钮管同一件事是陷阱),不再单独存在。
 *   等价关系:原 true  ≡ IDLE_KEEP_BIASED
 *             原 false ≡ IDLE_STOP_CONV
 *   现默认走第三种 IDLE_DISCONNECT —— 见下。
 */

/*
 * ══════════════════════════════════════════════════════════════════════
 * 轮次之间(idle)对电解池怎么处置 —— 三态,含实测代价
 * ══════════════════════════════════════════════════════════════════════
 * IDLE_STOP_CONV(旧行为,仅作对照):只写 CONVERT=0。放大器仍开,但芯片按 p40
 *   把 WE 切到"不转换"偏置通路(S1.n→VDD + 固定 50nA)。**实测这是最坏的中间态**:
 *   既不是恒电位保持,也不是开路;下一轮开跑必有 +385~474nA 还原冲击、~100s 恢复。
 *   (gap 长度已被排除:15.5s gap 反而给出最大瞬态,172.5s gap 的 C 模式最小)
 * IDLE_KEEP_BIASED:转换不停,电解池始终被钳在设定电位。轮次边界跳变实测 −1.7nA。
 *   代价:待机 3.5→7.3µA。这是 datasheet p1 "continuous biasing" 的原意。
 * IDLE_DISCONNECT(默认,2026-08-10 用户+生物负责人拍板):写 0x20 令
 *   WE/CE_AMP_EN=0 ⇒ **真开路**(p11 Output Off Leakage ±10pA typ/±100max)。
 *   与 CHI660 对齐:两次实验之间不驱动电解池,下次测量重新充双电层。
 *   🔴 代价必须承认:每轮开头必有完整双电层充电瞬态。实测 τ≈26–50s ⇒ 5τ≈130–250s。
 *      ⇒ **必须配 Quiet Time**(GUI 的 prestep_s 设 180~300s),先极化再记录,
 *         否则前 100s+ 数据是瞬态、不是稳态。CHI660 也是靠 Quiet Time,行为一致。
 *
 * 🔴 三态的值与 lib/afe_cfg.h 的 afe_idle_mode_t 同值(那里是权威定义),
 *    这里的三个宏保留只为让本文件的 switch 可读。可 SET idle= 改。
 */
#define IDLE_STOP_CONV   AFE_IDLE_STOP_CONV
#define IDLE_KEEP_BIASED AFE_IDLE_KEEP_BIASED
#define IDLE_DISCONNECT  AFE_IDLE_DISCONNECT
#define WP_DEFAULT_IDLE_MODE IDLE_DISCONNECT
#define WP_IDLE_MODE  cfg_live.idle

/*
 * idle 期间用 System ADC 直接量电极引脚电压,回答「不测量时电极被放在哪个电位」。
 * 这个问题我从框图推过两次、两次都被实验打脸(见上面两处 A/B 说明)⇒ 不再推,直接测。
 *
 * 🔴 必须同时量 WE 和 RE:E = V_WE − V_RE,而断开时 **RE 也在浮**,只测 WE 得不到 E。
 *    另加 CE(诊断:若被拉到地说明进了 sensor-detect 类状态)与 WO(放大器输出停靠点)。
 * 四路 tag:0xD0=WO1 / 0xD1=WE1 / 0xD2=RE1 / 0xD3=CE1(Table 9)。
 *
 * ⚠️ 仅在"转换已停"的两种 idle 模式下工作:手动单次转换与 IDLE_KEEP_BIASED
 *    "永不停转换"的定义直接冲突,那个模式下不探测(否则就破坏了它本身)。
 * ⚠️ 全断开时整个电解池对芯片 GND 浮动,电压可能跌破 0 或超量程 ⇒ 会削顶。
 *    所以日志里**同时打印原始 12-bit code**,只看 mV 看不出削顶。
 */
/* 🔬 逐词 trace 开关。判死"间歇 0"之后应关掉(它把电位上报量翻 4 倍)。 */
#define WP_CELLV_TRACE false
#define WP_DEFAULT_CELLV_ENABLE true
#define WP_CELLV_ENABLE cfg_live.cellv
#define WP_SYSADC_SENSV_GAIN MAX30131_SYSADC_GAIN_0P5X /* 0.5× ⇒ 可测 0~3.07V,盖住浮到 VDD */
/*
 * SYS_PERIOD(0x81 低半字节),与 SENS_PERIOD 同一张码表:
 *   0x0=124ms 0x1=242ms 0x2=476ms 0x3=945ms 0x4=1882ms …
 * 🔴 硬约束(p143):**四路的总转换时间必须 ≤ SYS_PERIOD**,否则置 INVALID_CFG,
 *   且"the conversion cycle abruptly restarts before completing all selected
 *   channels. Data saved in the FIFO is invalid for the interrupted channel."
 *   System ADC 单次 8.5ms(EC 表),SYS_CONV_TYPE=0 时每路要 offset+signal 两次
 *   ⇒ 4 路 × 2 × 8.5 ≈ 68ms。取 0x3(945ms)⇒ 占空比仅 ~7%,余量 13 倍。
 * ⚠️ 已知风险:System ADC 每 945ms 活动 68ms,可能给 pA 级电流测量注入扰动。
 *   若电流谱里出现 1/0.945 ≈ 1.06Hz 的谱线,就是它。把 WP_CELLV_ENABLE 置 false
 *   跑一轮即可 A/B 判定(那条路径下 idle 行为退回纯原始版本)。
 */
#define WP_DEFAULT_SYS_PERIOD_CODE 0x3U
#define WP_SYS_PERIOD_CODE cfg_live.sysper
/* IOFFSET_CONV:0 = 信号+offset(正常测量),1 = 仅 offset(标定/零点)。 */
#define WP_IOFFSET_CONV cfg_live.ioc

/*
 * 🔴 2026-08-09:0x0(31ms/12bit)→ 0x1(60ms/13bit)。
 *    动机是 50Hz 市电抑制,不是分辨率。积分窗对 f 的抑制 = |sinc(f·T)|,
 *    零点在 T = k/f;对 50Hz 即 T = k×20ms:
 *      0x0: 31ms = 1.55×20ms(离零点最远)⇒ 50Hz 仅衰减 −13.9 dB
 *      0x1: 60ms = 3.00×20ms(正中零点)  ⇒ 数学上完全抑制
 *    实测后果(2026-08-09,real-4.08uM-r2,1451 样本,去趋势 + 真实时间戳
 *    Lomb-Scargle):50Hz 被 fs=7.956Hz 折叠到 2.294Hz,|50−6×7.956|=2.263Hz,
 *    反推所需市电 = 50.031Hz ⇒ 认定为市电折叠。残差 std 4.9nA、峰峰 18nA
 *    = 37 个 LSB,**不是量化噪声**。
 *    白拿的两项:60ms < SENS_PERIOD 124ms ⇒ 8.06Hz 速率不损失;位数 12→13。
 *    ⚠️ 现实抑制受内部振荡器(标称 34.952kHz)精度与电网 ±0.05Hz 限制,
 *       时钟差 1% 即退化到约 −34 dB —— 仍比 −13.9 dB 好 20 dB。
 *    分析脚本:analysis/20260809-IT曲线周期波动/
 */
/*
 * 🔴🔴 2026-08-09 二次修正:必须按 FSR 分组选,**不能写死**。
 *    FSR 码 ≤3(50/100/250/500nA)走慢钟,>3(1000/2000nA)走 4× 快钟
 *    ⇒ 同一个 CONV_TIME 码,两组的积分时间差 4 倍
 *    (max30131.c 里 conv_slow_clk0_ms / conv_fast_clk0_ms 是两张表)。
 *
 *    在 SENS_PERIOD=0x0(124ms / 8.06Hz)前提下:
 *      码   位数   慢钟(≤500nA)          快钟(1/2µA)
 *      0x0   12    124ms  −30.4dB ✅      31ms  −13.9dB ✅
 *      0x1   13    241ms  ❌ 超 period     60ms  sinc 零点 ✅
 *      0x2   14    476ms  ❌ 超 period    119ms  −41.5dB ✅
 *
 *    ⇒ 慢钟组只有 0x0 可用,而它本来就有 −30.4dB,50Hz 从不是问题;
 *      需要处置的只有快钟组的 31ms/−13.9dB。
 *
 *    🔴 我第一版写死 0x1 是回归:FSR 一旦切到 ≤500nA,conv 241ms > period 124ms
 *      ⇒ INVALID_CFG,AFE 不出数、固件到不了 IT_START、采集器收 0 样本退 1。
 *      只在 FSR 2µA 下验证过就提交,是"验证条件比真实使用条件窄"的又一次重犯。
 *
 *    快钟组选 0x1 的依据与实测见 outputs/20260809-IT曲线周期波动/README.md
 *    (残差 std 4.905→0.907nA,2.29Hz 峰功率 −23.0dB)。
 *    ⚠️ 0x2(119ms/14bit/−41.5dB,同样塞得进 124ms)可能更优:多一位、且靠
 *      sinc 包络而非精确零点,对时钟误差更钝。**未实测,不盲改。**
 */
/*
 * 🔴🔴 2026-08-10 结构性修复:CONV_TIME **不再是独立旋钮**,而是派生量。
 *
 * 上面那两段说明记录了同一个坑被踩两次的过程(先写死 0x1、再改成按 FSR 分组的
 * 三元表达式)。根因不是选错值,而是**把一个由 FSR/period/时钟源共同决定的量
 * 当成了独立参数**。现在交给 lib 的 max30131_auto_conv_code():
 *   排序键 = ① 最坏情况 50Hz 抑制 ② 位数 ③ idle 窗口小
 * 并在审计行里打出 `conv_src=auto|pin` 与次优码 `conv_alt`,让"为什么选它"可查。
 *
 * 现行配置(FSR 1µA 快钟组 + SENS_PERIOD 0x0 = 124ms)由此派生出 **0x2**
 * (实测表值,不是估的 —— lib 的两张 11×4 表):
 *      码    积分     位数   50Hz 标称   50Hz 最坏(±2% 钟)   idle 窗口
 *     0x0    31ms     12     −13.3dB      −13.3dB              75.0%
 *     0x1    60ms     13     −32.4dB      −27.2dB              51.5%   ← 旧值
 *     0x2   119ms     14     −32.7dB      −27.9dB               4.4%   ← 现值
 *     0x3   236ms     15         装不进 124ms 的 SENS_PERIOD
 *   ⇒ 0x2 相对 0x1:位数 +1、最坏抑制 +0.7dB、idle 窗口 51.5%→4.4%。三项皆优。
 *   ⚠️ 注意 0x1 那个"sinc 精确零点"的说法(上一段)**已作废**:它算的是转换时间,
 *      而抑制只由**积分时间**决定(相差 246 个 precharge 时钟)。真值 −32.4dB,
 *      不是 −44.7dB;判据是我们自己的实测(0x0→0x1 降 23.0dB,积分口径预测 19.1dB、
 *      转换口径预测 30.9dB)。
 * 想钉住某个码用 `SET conv=<码>`(审计行会标 conv_src=pin)。
 */
#define WP_CONV_TIME_CODE cfg_live.conv
#define WP_SENS_PERIOD_CODE cfg_live.period

/* 每批攒多少样本再取。轮询模式下这只决定读取粒度,不再是唤醒条件。 */
#define WP_BATCH_SAMPLES 16U

/* 本轮 i-t 试验的电位保持时间。到时后固件停转换并保持配置的空闲电位。 */
#define WP_MEASUREMENT_DURATION_MS GUI_MEASUREMENT_DURATION_MS
/*
 * 🔴 采样周期现在是运行时量 ⇒ 期望样本数也必须运行时算,不能再用 GUI_SENS_PERIOD_MS
 *    这个编译期常量。`SET period=` 改完周期而样本数还按旧周期算,会让一轮的时长
 *    悄悄变成几倍或几分之一(轮次时长与样本数是两个独立的收尾条件)。
 */
static uint32_t expected_sample_count(void)
{
	int32_t p = drv_live.period_ms > 0 ? drv_live.period_ms : 1;

	return (uint32_t)((WP_MEASUREMENT_DURATION_MS + (uint32_t)p - 1U) / (uint32_t)p);
}

/* 标定阶段暂时放慢积分,让 500nA 参考档也有完整转换时间;正式测量再切回快速档。 */
#define CAL_CONV_TIME_CODE         0x4U
#define CAL_SENS_PERIOD_CODE       0x5U

/* 轮询间隔:比一个采样周期短,保证不漏 FIFO(256 深)。 */
#define POLL_INTERVAL_MS 20

/*
 * 饱和预警余量(counts):离边界还有多少 counts 就开始报 sat,给上位机反应余地。
 *
 * 🔴 2026-08-09 改:原为写死 `1311U`(= 2% FS)。那个值是按"offset 占满量程
 *    一半"这类大 offset 场景定的。**offset 小时余量会超过 offset 本身**,于是
 *    零电流附近整个有效测量区都被误判成饱和:
 *      FSR 1µA + offset 19nA ⇒ 1311 counts = 20.0 nA > offset 19 nA
 *      ⇒ 判据退化成「I ≥ −1.0 nA 就报 sat」
 *      ⇒ 末段 −2~−3 nA 的真实数据紧贴阈值,噪声让 counts 反复跨过 1311,
 *        界面上红黑交替(实测该轮 126 个样本属此类误报;另有 728 个是**真**
 *        饱和 —— counts 恒为 8、连续 91.3 s 顶在 offset 轨上,那部分不是误报)。
 *
 *    改为按**零电流码**(= offset×2^16/FSR)的固定比例取。这样余量在**电流**
 *    维度上恒等于 offset 的 5%,与 FSR 选哪档无关,物理含义一致。
 *    保留 1311 作上限 ⇒ 大 offset 配置(如 50%FS)的行为与改动前逐位一致,
 *    不引入回归。下限 8 counts = 13bit 的一个码步长,保证真饱和一定报得出来。
 *    比例本身现在也可运行时改(`SET satpct=`,0 表示不预警)。
 *
 * 🔴 2026-08-10:计算搬进 lib 的 afe_cfg_derive(),本函数只是取值。
 *    单一来源很重要 —— 余量必须与 `CFG_DERIVED` 行里打出的 `sat_margin` 逐位相同,
 *    否则界面上的红点与日志里的阈值会对不上,而这正是 2026-08-09 那次
 *    "余量误报看起来和真饱和一模一样"绕大圈的原因。
 */
#define SAT_MARGIN_DEFAULT_PCT 5      /* 取零电流码的 5% */

static uint16_t sat_margin_counts(void)
{
	return drv_live.sat_margin;
}

/* ================================================================== */
/*
 * 标定状态。开机双档标定后填入,轮询换算全用它们。
 * 🔴 fsr_cal_pa 是**标定后的 FSR 真值**,不是标称 50000。
 * 🔴 baseline_counts 是同档 offset-only 的 counts —— 差分换算用它,
 *    这样 ADC 固有偏移(±80 LSB)与 offset 源容差(±22%)双双抵消。
 */
static int32_t fsr_cal_pa;          /* 0 = 未标定,退回标称值 */
static uint16_t baseline_counts;    /* 0 = 未测,退回绝对 offset 换算 */
static bool cal_valid;

static uint32_t seq;
static uint8_t last_sat; /* 只在饱和状态翻转时告警 */

/*
 * 上报一行。格式**必须**与 host/pa_host/record.py 的 LINE_RE 逐字对齐:
 *   S seq=123 ms=456789 counts=13107 fa=2500000 tag=0 auto=1 ovf=0
 *   S seq=123 ms=456789 counts=13107 fa=2500000 tag=0 auto=1 ovf=0 sat=0 ep=3
 * 🔴 `ep=` 是配置纪元。record.py 的 LINE_RE 把它作为**可选尾组**,所以旧日志
 *    照样可解析;但新数据里没有它 == 固件版本不对,不该静默当成 ep=0。
 * 🔴 电流单位是 **整数 fA** 不是 pA —— 50nA 档 LSB 约为 763 fA,
 *    用 pA 会让协议本身比器件还粗、把亚 pA 噪声量化掉。
 * 用 printk 而不是 LOG_*:LOG 会加时间戳/等级前缀,破坏行协议。
 * 诊断信息仍走 LOG_*,record.py 会忽略不匹配的行。
 */
static void emit_sample(uint16_t counts, int32_t fa, uint8_t tag, bool auto_mode,
			uint8_t ovf, uint8_t sat)
{
	printk("S seq=%u ms=%u counts=%u fa=%d tag=%u auto=%u ovf=%u sat=%u ep=%u\n",
	       seq++, (uint32_t)k_uptime_get_32(), counts, fa, tag,
	       auto_mode ? 1U : 0U, ovf, sat, cfg_epoch);
}

/*
 * ══════════════════════════════════════════════════════════════════════
 * STATUS1 的**读清**位:必须走 sticky 累积器
 * ══════════════════════════════════════════════════════════════════════
 * 🔴 VDD_OOR 与 PWR_RDY 都是"读 0x00 即清"(datasheet p82)。而本固件里读 0x00 的
 *    地方不止一处:drain_fifo_for_voltages() 在 idle 每 20ms 读一次、
 *    manual_convert_once() 轮询读、afe_status_poll() 1Hz 读。
 *    ⇒ 20ms 那条会在 1Hz 监视看到之前把故障位吃光,监视器永远报"一切正常"。
 *    这正是"沉默即故障"公理要防的形态:**看不到 ≠ 没发生**。
 * ⇒ 所有读 STATUS1 的地方一律走 status1_read();它把两个读清位 OR 进 sticky,
 *   由 afe_status_poll() 统一消费并清零。
 */
/*
 * 🔴 审计行的格式化缓冲**必须是静态的,不能放栈上**(2026-08-10 实测教训)。
 * 曾经 afe_cfg_commit() 与 handle_command_line() 各持一个 char[512],两帧同时活着
 * ⇒ 光缓冲就 1KB,加上 afe_cfg_t/afe_derived_t/afe_plan_t 约 500B,再叠 printk 自身
 * 的格式化开销,直接把 CONFIG_MAIN_STACK_SIZE=2048 撑爆:
 *   ZEPHYR FATAL ERROR 2: Stack overflow ⇒ Halting system(发一条 OCP 就整机死)
 * 实测最长行 CFG_DERIVED = 430 字符 ⇒ 640 有 1.5× 余量。
 * 安全性:四个格式化器都是"填缓冲 → 立刻 printk"顺序使用,同一线程、无嵌套、
 * 无重入(ocp_run 期间不格式化审计行),所以共享一个缓冲是安全的。
 */
#define AUDIT_LINE_MAX 640
static char audit_line[AUDIT_LINE_MAX];

static uint8_t status1_sticky;

#define STATUS1_STICKY_MASK \
	(BIT(MAX30131_STATUS1_VDD_OOR_Pos) | BIT(MAX30131_STATUS1_PWR_RDY_Pos))

static int status1_read(uint8_t *out)
{
	int rc = max30131_spi_read_reg(MAX30131_REG_STATUS1, out);

	if (rc == 0) {
		status1_sticky |= (uint8_t)(*out & STATUS1_STICKY_MASK);
	}
	return rc;
}

/* ------------------------------------------------------------------ */
/* AFE 初始化                                                          */
/* ------------------------------------------------------------------ */
static int afe_probe(void)
{
	uint8_t part_id = 0;
	int rc = max30131_spi_read_reg(MAX30131_REG_PART_ID, &part_id);

	if (rc) {
		return rc;
	}
	/*
	 * PART_ID 是**唯一**能证明 SPI 帧格式写对了的手段(纯逻辑层无法自证:
	 * 地址/命令字节的拼法只有真器件会拒绝)。读不到就别往下走。
	 */
	LOG_INF("MAX30131 PART_ID = 0x%02x", part_id);
	if (part_id == 0x00U || part_id == 0xFFU) {
		LOG_ERR("🔴 PART_ID 非法(0x%02x)—— SPI 未通或帧格式错。"
			"先查 CSN=P0.11 / SCLK=P0.05 / MOSI=**P1.09** / MISO=P0.04",
			part_id);
		return -EIO;
	}
	return 0;
}

/* 写 DAC 后立即回读。任何写错、丢写或寄存器位序错误都会阻止测量启动。 */
static int write_dac_verified(uint8_t msb_reg, uint8_t enlsb_reg,
			      uint16_t code, const char *name)
{
	uint8_t expected_msb = 0U, expected_enlsb = 0U;
	uint8_t actual[2] = { 0U, 0U };

	max30131_enc_dac(code, true, &expected_msb, &expected_enlsb);
	if (max30131_spi_write_reg(msb_reg, expected_msb) != 0 ||
	    max30131_spi_write_reg(enlsb_reg, expected_enlsb) != 0 ||
	    max30131_spi_read_burst(msb_reg, actual, sizeof(actual)) != 0) {
		LOG_ERR("%s DAC 写入/回读失败", name);
		return -EIO;
	}
	if (actual[0] != expected_msb || actual[1] != expected_enlsb ||
	    max30131_dec_dac_code(actual[0], actual[1]) != code) {
		LOG_ERR("%s DAC 回读不一致:期望 code=0x%03x bytes=%02x/%02x,"
			"实际 bytes=%02x/%02x", name, code, expected_msb,
			expected_enlsb, actual[0], actual[1]);
		return -EIO;
	}
	return 0;
}

static int afe_configure(void)
{
	int rc;

	/* --- 先让模拟域完整关断再软复位,避免上一次过载状态跨 MCU 复位残留 --- */
	rc = max30131_spi_write_reg(MAX30131_REG_SYSTEM_CONTROL,
				    max30131_enc_system_control(false, true, false, WP_CLK_40K));
	if (rc) {
		return rc;
	}
	k_msleep(100);
	rc = max30131_spi_write_reg(MAX30131_REG_SYSTEM_CONTROL,
				    max30131_enc_system_control(false, false, false, WP_CLK_40K));
	if (rc) {
		return rc;
	}
	k_msleep(100);

	/* --- 软复位,回到已知状态 --- */
	rc = max30131_spi_write_reg(MAX30131_REG_SYSTEM_CONTROL,
				    max30131_enc_system_control(true, false, false, false));
	if (rc) {
		return rc;
	}
	k_msleep(10);
	rc = max30131_spi_write_reg(MAX30131_REG_SYSTEM_CONTROL,
				    max30131_enc_system_control(false, false, false, WP_CLK_40K));
	if (rc) {
		return rc;
	}
	k_msleep(10);

	/* --- 通道 1 配置(hex 定版,经 workflow critic,见 05 文档)--- */
	/*
	 * hex 定版来自 05 文档(经 workflow critic),且**每一个值都在单测里断言**
	 * (tests/test_max30131.c 的 test_register_hex_matches_doc)。
	 *
	 * 🔴 地址与名字务必对上 —— 我曾把注释写串位(把 CE_DAC_MX 标到 0x68、
	 * 把 REFERENCE_CONTROL 标到 0x80),值虽对但注释误导,本项目里这就是隐患。
	 *
	 * 🔴 `0x83 CONVERT START` **刻意不在这张表里** —— 它一写就启动 AUTO,
	 * 而开机自检要用手动转换(AUTO=0 + CONVERT=1),AUTO=1 下手动请求会被**静默忽略**。
	 * 由 afe_start_auto() 在自检之后写。
	 *
	 * `0x05 INT ENABLE1 = 0x80`(A_FULL_EN)**刻意不写** —— 本板 INTB 悬空,
	 * 没有中断线可用(见 07 文档 §2.1),开中断使能没有意义。
	 */
	const struct {
		uint8_t addr;
		uint8_t val;
		const char *what;
	} cfg[] = {
		{ 0x20U, 0xC5U,
		  "S1_CONFIG1: WE/CE_AMP_EN=1, WE_DAC_MX=00→DACA, "
		  "🔴CE_DAC_MX=01→DACB(不写则两放大器共用 DACA、E=0), CHOP_EN=1" },
		{ 0x21U, 0x90U, "S1_CONFIG2: 3 端电极 + WE drive" },
		/*
		 * 电极电压连采(System ADC)。常开 —— idle 与测量期间一视同仁,
		 * 靠 AUTO 模式与 Sensor ADC 并行(p143),共用 FIFO 靠 tag 区分。
		 * 次序:先配增益与通道,最后开 SYS_SELECT。
		 */
		{ MAX30131_REG_SYS_ADC_SETUP,
		  WP_CELLV_ENABLE ? max30131_enc_sys_adc_setup(WP_SYSADC_SENSV_GAIN) : 0x00U,
		  "SYS ADC SETUP: SENSV 增益 0.5×(可测 0~3.07V), 🔴OPA_BYPASS_EN=0" },
		{ MAX30131_REG_CONVERT_SETUP2,
		  WP_CELLV_ENABLE ? (uint8_t)(WP_SYS_PERIOD_CODE << MAX30131_CS2_SYS_PERIOD_Pos)
				  : 0x00U,
		  "CONVERT SETUP2: SYS_PERIOD(四路总转换时间须 ≤ 该周期)" },
		{ MAX30131_REG_SYS_ADC_IN_SEL2,
		  WP_CELLV_ENABLE ? (uint8_t)(BIT(MAX30131_SYSADC_S1_WE_SEL_Pos) |
					      BIT(MAX30131_SYSADC_S1_RE_SEL_Pos) |
					      BIT(MAX30131_SYSADC_S1_CE_SEL_Pos) |
					      BIT(MAX30131_SYSADC_S1_WO_SEL_Pos))
				  : 0x00U,
		  "SYS ADC IN SEL2: WE1+RE1+CE1+WO1(E=V_WE−V_RE 必须两路都有)" },
		{ MAX30131_REG_SYS_ADC_IN_SEL1,
		  WP_CELLV_ENABLE ? (uint8_t)BIT(MAX30131_SYSADC_SYS_SELECT_Pos) : 0x00U,
		  "SYS ADC IN SEL1: SYS_SELECT(⚠️ 位号未从 datasheet 文本层确认,见 regs.h)" },
		{ 0x22U, WP_S1_CONFIG3, "S1_CONFIG3:IOS_MODE(见文件头 A/B 说明)" },
		{ 0x23U, max30131_enc_s1_config4(WP_FSR, WP_OFFSET_SEL),
		  "S1_CONFIG4: configured FSR/offset" },
		/* 🔴 改用编码函数而非字面量 —— WP_CONV_TIME_CODE 现在随 FSR 分组变,
		 * 写死会和它脱钩。10Hz 工作流跳过双档标定,真正生效的是这张表,
		 * 不是 restore 路径那次写,所以这里必须自适应。 */
		{ 0x24U, max30131_enc_s1_config5(WP_CONV_TIME_CODE, true),
		  "S1_CONFIG5: CONV_TIME 随 FSR 分组(慢钟 0x0/12bit,快钟 0x1/13bit)" },
		{ 0x68U, 0x01U, "REFERENCE CONTROL: 内部基准 1.536V + REF_EN=1" },
		/*
		 * 🔴 2026-08-10 补:不写这个寄存器,STATUS1.VDD_OOR **恒为 0**
		 * (p82:"otherwise, VDD_OOR is always set to 0")⇒ 掉压监测是死的,
		 * 而本固件此前一直没写它 ⇒ 之前所有 "VDD_OOR=0" 都不构成"供电正常"的证据。
		 * REF_EN 已在上一行置 1,两个条件这才同时满足。
		 * INTB_OCFG=00 开漏:本板 INTB 悬空,开漏保证它永不驱动。
		 */
		{ MAX30131_REG_INTB_SETUP,
		  (uint8_t)(BIT(MAX30131_INTB_EN_VDD_OOR_Pos) |
			    (MAX30131_INTB_OCFG_OPEN_DR << MAX30131_INTB_OCFG_Pos)),
		  "INTB SETUP: EN_VDD_OOR=1(否则 VDD_OOR 恒 0)+ 开漏" },
		/* 🔴 原为字面量 0x00,那默认了 SENS_PERIOD=0。period 现在可运行时改,
		 * 写死会让开机那一刻的周期与 cfg_live 不符(而 cfg_live 决定换算与
		 * 期望样本数)⇒ 必须走编码函数。 */
		{ 0x80U, max30131_enc_convert_setup1(false, WP_IOFFSET_CONV, false,
						     WP_SENS_PERIOD_CODE),
		  "CONVERT SETUP1: DC + IOFFSET_CONV + SENS_PERIOD(随 cfg_live)" },
	};

	for (size_t i = 0; i < ARRAY_SIZE(cfg); i++) {
		rc = max30131_spi_write_reg(cfg[i].addr, cfg[i].val);
		if (rc) {
			LOG_ERR("write %s (0x%02x) failed", cfg[i].what, cfg[i].addr);
			return rc;
		}
	}

	/* 🔴 基准建立需 ≥12ms(datasheet);抢跑会读到未稳定的值 */
	k_msleep(20);

	/* --- 极化电位:E = V_DACA − V_DACB --- */
	max30131_polarization_t pol;
	max30131_err_t e = max30131_polarization_from_e(WP_V_WE_MV, WP_STARTUP_E_MV,
							max30131_ref_mv(WP_REF), &pol);

	if (e != MAX30131_OK) {
		LOG_ERR("🔴 启动电位算不出来(err=%d):V_WE=%dmV E=%dmV",
			(int)e, WP_V_WE_MV, WP_STARTUP_E_MV);
		return -EINVAL;
	}
	LOG_INF("DACA code=0x%03x (V_WE=%d mV) / DACB code=0x%03x (V_RE=%d mV) / E=%d mV",
		pol.code_a, pol.v_dac_a_mv, pol.code_b, pol.v_dac_b_mv, WP_STARTUP_E_MV);

	/*
	 * 🔴 DAC 是「MSB 字节 + EN/LSB 字节」的非直觉布局:
	 *   0x69 = CODE[11:4] / 0x6A = CODE[3:0]<<4 | 保留 | EN(bit0)
	 * 按 `hi=code>>8, EN@bit7` 写会把电位设成完全错的值(恒电位仪失效)。
	 * 编码交 lib,已单测(断言 0x640 → msb=0x64 / lsb=0x01)。
	 */
	if (write_dac_verified(MAX30131_REG_DACA_MSB, MAX30131_REG_DACA_ENLSB,
			       pol.code_a, "DACA") != 0 ||
	    write_dac_verified(MAX30131_REG_DACB_MSB, MAX30131_REG_DACB_ENLSB,
			       pol.code_b, "DACB") != 0) {
		return -EIO;
	}

	/* DAC 建立 ~10ms */
	k_msleep(15);

	/* --- 共模/裕量校核:算得出来 ≠ 器件受得了 --- */
	e = max30131_check_headroom(&pol, 3000 /* VDD mV,CR2032 标称 */);
	if (e != MAX30131_OK) {
		LOG_WRN("⚠️ 共模裕量校核不过(err=%d)—— 电池跌到 EOL 时更紧,留意基线漂移",
			(int)e);
	}

	/* --- offset 是否盖得住信号峰(还原方向的硬约束)--- */
	int32_t fsr_pa = max30131_fsr_pa(WP_FSR);
	int32_t off_pa = max30131_offset_pa(WP_OFFSET_SEL, WP_FSR);

	e = max30131_check_offset_covers_signal(off_pa, 5000 /* 5nA 信号峰 */, 50);
	if (e != MAX30131_OK) {
		LOG_ERR("🔴 offset(%d pA)盖不住信号峰 —— WEn 会被顶出设定电位、"
			"失去恒电位控制并钳在 0nA",
			off_pa);
		return -ERANGE;
	}

	/* --- FIFO watermark --- */
	/*
	 * 🔴 FIFO_A_FULL 的语义是「中断前还剩几个空位」,不是「攒够几个样本」:
	 *    A_FULL 置位时 FIFO 内样本数 = 256 − FIFO_A_FULL。
	 *    想每 16 个样本一批 ⇒ 必须写 240(0xF0),写 16 会变成 240 个样本 ≈15 分钟。
	 * 本板 INTB 悬空、走轮询,watermark 只影响状态位;仍按正确语义设,
	 * 将来飞线接了 INTB 不用改这里。
	 */
	uint8_t a_full = max30131_fifo_a_full_from_batch(WP_BATCH_SAMPLES);

	LOG_INF("FIFO_A_FULL = %u(= 256 − %u 样本)", a_full, WP_BATCH_SAMPLES);
	rc = max30131_spi_write_reg(MAX30131_REG_FIFO_CONFIG1, a_full);
	rc |= max30131_spi_write_reg(MAX30131_REG_FIFO_CONFIG2,
				     max30131_enc_fifo_config2(true, true, false, true));
	if (rc) {
		return -EIO;
	}

	/* --- 时序自洽:采样周期不得短于转换时间 --- */
	/*
	 * 🔴 签名是 (conv_code, period_code, clk_sel_40k, fsr) —— 四参数。
	 * 第 3 个是**时钟源**(34.952k vs 40.96k),不是"快钟组";
	 * 快慢钟分组由函数自己从 fsr 推导。传错位置会静默算出错的转换时间。
	 */
	e = max30131_check_period_vs_conv(WP_CONV_TIME_CODE, WP_SENS_PERIOD_CODE,
					  WP_CLK_40K, WP_FSR);
	if (e != MAX30131_OK) {
		LOG_ERR("🔴 SENS_PERIOD 短于 CONV_TIME(err=%d)", (int)e);
		return -EINVAL;
	}
	LOG_INF("conv=%d ms / period=%d ms ⇒ %d mSPS",
		max30131_conv_time_ms(WP_CONV_TIME_CODE, WP_CLK_40K,
				      max30131_fsr_uses_fast_clock(WP_FSR)),
		max30131_sens_period_ms(WP_SENS_PERIOD_CODE, WP_CLK_40K),
		1000000 / max30131_sens_period_ms(WP_SENS_PERIOD_CODE, WP_CLK_40K));

	/*
	 * 🔴 **不在这里启动 AUTO** —— 开机自检要用手动转换(AUTO=0 + CONVERT=1),
	 * 而 05 文档明确:手动转换必须在 AUTO=1 **之前**做(AUTO=1 下手动请求被静默忽略)。
	 * AUTO 由 afe_start_auto() 在自检之后启动。
	 */
	/*
	 * 🔴 两个 LSB 必须都打出来。max30131_lsb_fa() 给的是 **16 位帧** 的 LSB
	 *    (FSR/2^16),但 CONV_TIME 决定的实际位数 n<16 时,结果左对齐进 16 位帧、
	 *    低 (16−n) 位恒为 0 ⇒ 真实量化台阶是它的 2^(16−n) 倍。
	 *    2026-08-09 踩过:只打帧 LSB 会让分辨率看起来好 8~16 倍
	 *    (实证:1504 个样本的 counts 无一例外是 16 的倍数 @12bit)。
	 */
	uint8_t adc_bits = max30131_conv_time_bits(WP_CONV_TIME_CODE);
	int32_t lsb_frame_fa = max30131_lsb_fa(WP_FSR);
	int32_t lsb_eff_fa = lsb_frame_fa << (16U - adc_bits);
	LOG_INF("AFE 就绪:FSR=%d pA / offset=%d pA / %u bit / LSB 有效=%d fA(帧 %d fA)",
		fsr_pa, off_pa, adc_bits, lsb_eff_fa, lsb_frame_fa);
	/* 🔴 把两个方向的可测上限显式打出来 —— 别让"量程够不够"停留在文档里 */
	int32_t off_lo = 0, off_hi = 0;

	max30131_offset_range_pa(WP_OFFSET_SEL, WP_FSR, &off_lo, &off_hi);
	LOG_INF("可测量程:还原 ≤%d pA(=offset,最坏 min 档只有 %d pA)/ 氧化 ≤%d pA",
		max30131_max_reduction_pa(off_pa), max30131_max_reduction_pa(off_lo),
		max30131_max_oxidation_pa(fsr_pa, off_pa));
	/* 🔴 把 sat 预警阈值显式打出来 —— 2026-08-09 踩过:阈值不可见时,
	 * 余量误报看起来和真饱和一模一样(界面都是红点),排查绕了一大圈。 */
	{
		uint16_t sm = sat_margin_counts();
		int32_t sm_pa = (int32_t)(((int64_t)sm * fsr_pa) / 65536);

		LOG_INF("sat 预警余量:%u counts = %d pA ⇒ 还原侧 I ≥ %d pA 报 sat / "
			"氧化侧 I ≤ %d pA 报 sat", sm, sm_pa,
			max30131_max_reduction_pa(off_pa) - sm_pa,
			-(max30131_max_oxidation_pa(fsr_pa, off_pa) - sm_pa));
	}
	LOG_INF("参考:左旋多巴标定实测稳态最大 12800 pA(浓度 50)⇒ %s",
		max30131_max_reduction_pa(off_lo) > 12800 ? "最坏档位也够 ✅"
							 : "🔴 余量不足,查 offset 档位");
	return 0;
}

static int __attribute__((unused)) afe_start_auto(void)
{
	int rc = max30131_spi_write_reg(MAX30131_REG_CONVERT_START,
					max30131_enc_convert_start(true, true));
	if (rc == 0) {
		LOG_INF("AUTO 自主转换已启动");
	}
	return rc;
}

/*
 * 🔴 **重起** AUTO(先 AUTO=0 再 AUTO=1)。
 *
 * 2026-08-10 实测答了一个我在 apply_range() 注释里标着「未知」的问题:
 *   ⚠️「AUTO=1 运行中写 0x23/0x24 是否立即生效」
 * ⇒ **0x24 的 SELECT 位不生效**:通道选择在一次 AUTO 序列启动时锁存,
 *   运行中改它不会被重新采纳,而且**没有任何错误指示**。
 *
 * 实测证据:第 1 轮(开机后首次启 AUTO,SELECT 本来就是 1)拿到 536 个电流样本;
 * 第 2 轮 idle 期间 set_sensor_selected(false) → 退出 idle 写回 true → 电流样本
 * **0 个**,而电位词(System ADC)照常每秒一组。`POTENTIAL_AUDIT sample=0` 刷了
 * 整轮,STATUS1 也一切正常 —— 这是个纯静默失败。
 *
 * ⇒ 凡是改过 SELECT,就必须重起 AUTO 让它重新锁存。
 * ⚠️ 代价:重起那一瞬间转换会停一次,而"停转换"实测会扰动电解池
 *   (p40 的固定 50nA 偏置通路,见文件头 A/B 二)。所以只在**退出 idle 时**做 ——
 *   那时正要重新极化,一个瞬时扰动无所谓;IDLE_KEEP_BIASED 下从不改 SELECT,
 *   也就从不走这条路径,它"转换从不停"的语义得以保持。
 */
static int afe_restart_auto(void)
{
	int rc = max30131_spi_write_reg(MAX30131_REG_CONVERT_START,
					max30131_enc_convert_start(false, false));

	if (rc) {
		return rc;
	}
	k_msleep(2);
	return afe_start_auto();
}

/* ================================================================== */
/* 🔬 开机自检:判定「开路却读到 ~1.2nA」到底是什么                     */
/* ================================================================== */
/*
 * 背景(2026-07-31 首烧实测):电极口开路时读到 1.209 nA。
 * 零电流本应 counts = offset×2¹⁶/FSR = 10000×65536/50000 = 13107,实测 11522。
 * 等价于 ADC 认为 offset 源只有 8790 pA(标称 10000,偏低 12.1%)。
 *
 * 开路时 CE–溶液–WE 回路断开、**恒电位环无法闭合**,所以该读数不是任何真实电池电流。
 * 两个候选原因,后果差别极大:
 *   A. 内部 offset 源容差 —— 无害,标定减掉即可
 *   B. 真实漏电(0.2V/1.21nA ⇒ ≈165MΩ,助焊剂/连接器/板面)—— 要紧:
 *      目标信号 0.5–5nA,1.2nA 基线 = 信号的 24–240%,且随温湿度漂
 *
 * 🔴 判决依据:**真实漏电是欧姆的(I ∝ E),内部 offset 源与 E 完全无关。**
 *   E=0 时若读数仍 ~1.2nA  ⇒ A(offset 容差)
 *   E=0 时若读数塌到 ~0    ⇒ B(漏电)
 *   介于两者 ⇒ A+B 混合,按斜率分离
 *
 * 外加 IOFFSET_CONV=01 的 offset-only 转换(读 0x2B/0x2C S1_IOFFSET)直接量 offset 源真值,
 * 与 E 扫互为**独立**佐证 —— 两条独立证据同向才算定性(教训见 troubleshooting:
 * 同源失败不算独立证据)。
 */

/* 手动单次转换:AUTO=0 + CONVERT=1,仅用于开机自检/传统 AFE 标定。 */
static int manual_convert_once(uint16_t *counts_out, uint8_t *tag_out)
{
	int rc = max30131_spi_write_reg(MAX30131_REG_FIFO_CONFIG2,
					max30131_enc_fifo_config2(true, true, false, true));
	if (rc) {
		return rc;
	}
	rc = max30131_spi_write_reg(MAX30131_REG_CONVERT_START,
				    max30131_enc_convert_start(false, true));
	if (rc) {
		return rc;
	}

	/* CONV_TIME=0:目标量程的最短积分时间;给 3s 上限。 */
	for (int i = 0; i < 600; i++) {
		k_msleep(5);
		uint8_t st = 0;

		if (status1_read(&st)) {
			return -EIO;
		}
		if (st & BIT(MAX30131_STATUS1_FIFO_DATA_RDY_Pos)) {
			uint8_t raw[3];

			if (max30131_spi_read_burst(MAX30131_REG_FIFO_DATA, raw, sizeof(raw))) {
				return -EIO;
			}
			max30131_fifo_word_t w;

			if (max30131_fifo_unpack(raw, &w) != MAX30131_OK) {
				return -EIO;
			}
			*counts_out = w.counts;
			*tag_out = w.tag;
			return 0;
		}
	}
	LOG_ERR("手动转换超时(3s 内 FIFO_DATA_RDY 未置位)");
	return -ETIMEDOUT;
}

static void log_status1(const char *when)
{
	uint8_t st = 0;

	if (status1_read(&st)) {
		LOG_ERR("STATUS1 读失败");
		return;
	}
	LOG_INF("STATUS1%s = 0x%02x  [A_FULL=%d DATA_RDY=%d AC_RDY=%d EIS_CAL=%d "
		"🔴INVALID_CFG=%d 🔴VDD_OOR=%d PWR_RDY=%d]",
		when, st,
		(st >> MAX30131_STATUS1_A_FULL_Pos) & 1,
		(st >> MAX30131_STATUS1_FIFO_DATA_RDY_Pos) & 1,
		(st >> MAX30131_STATUS1_AC_DATA_RDY_Pos) & 1,
		(st >> MAX30131_STATUS1_EIS_CAL_DONE_Pos) & 1,
		(st >> MAX30131_STATUS1_INVALID_CFG_Pos) & 1,
		(st >> MAX30131_STATUS1_VDD_OOR_Pos) & 1,
		(st >> MAX30131_STATUS1_PWR_RDY_Pos) & 1);

	if (st & BIT(MAX30131_STATUS1_INVALID_CFG_Pos)) {
		LOG_ERR("🔴 INVALID_CFG 置位 —— 配置本身无效,后面所有读数都不可信!"
			"最常见原因:SENS_PERIOD < CONV_TIME");
	}
	if (st & BIT(MAX30131_STATUS1_VDD_OOR_Pos)) {
		LOG_ERR("🔴 VDD_OOR 置位 —— 供电超出器件范围");
	}
}

/* 把极化设成指定 E(mV),返回是否成功 */
static int set_polarization(int32_t e_mv)
{
	max30131_polarization_t pol;

	if (max30131_polarization_from_e(WP_V_WE_MV, e_mv,
					 max30131_ref_mv(WP_REF), &pol) != MAX30131_OK) {
		LOG_ERR("E=%d mV 算不出极化码", e_mv);
		return -EINVAL;
	}

	if (write_dac_verified(MAX30131_REG_DACA_MSB, MAX30131_REG_DACA_ENLSB,
			       pol.code_a, "DACA") != 0 ||
	    write_dac_verified(MAX30131_REG_DACB_MSB, MAX30131_REG_DACB_ENLSB,
			       pol.code_b, "DACB") != 0) {
		return -EIO;
	}
	k_msleep(15); /* DAC 建立 ~10ms */
	int32_t actual_a_mv = max30131_dac_mv_from_code(pol.code_a,
							max30131_ref_mv(WP_REF));
	int32_t actual_b_mv = max30131_dac_mv_from_code(pol.code_b,
							max30131_ref_mv(WP_REF));
	LOG_INF("电位回读通过:E目标=%d mV,E量化=%d mV,DACA=0x%03x,DACB=0x%03x",
		e_mv, actual_a_mv - actual_b_mv, pol.code_a, pol.code_b);
	return 0;
}

/*
 * 采集中只读审计 DAC 寄存器,不重写电位。连续两次回读不一致才判故障,
 * 以免把一次 SPI 传输错误误报为真实的电位寄存器跳变。
 */
static int audit_polarization(int32_t e_mv, uint32_t sample_count)
{
	max30131_polarization_t expected;
	uint8_t daca_raw[2] = { 0U, 0U };
	uint8_t dacb_raw[2] = { 0U, 0U };
	uint16_t daca = 0U;
	uint16_t dacb = 0U;
	uint8_t s1_config1 = 0U;
	uint8_t reference_control = 0U;
	uint8_t system_control = 0U;

	if (max30131_polarization_from_e(WP_V_WE_MV, e_mv,
					 max30131_ref_mv(WP_REF), &expected) != MAX30131_OK) {
		return -EINVAL;
	}

	for (uint8_t attempt = 0U; attempt < 2U; attempt++) {
		int rc_a = max30131_spi_read_burst(MAX30131_REG_DACA_MSB, daca_raw,
						     sizeof(daca_raw));
		int rc_b = max30131_spi_read_burst(MAX30131_REG_DACB_MSB, dacb_raw,
						     sizeof(dacb_raw));
		int rc_s1 = max30131_spi_read_reg(MAX30131_REG_S1_CONFIG1, &s1_config1);
		int rc_ref = max30131_spi_read_reg(MAX30131_REG_REFERENCE_CONTROL,
						      &reference_control);
		int rc_sys = max30131_spi_read_reg(MAX30131_REG_SYSTEM_CONTROL,
						      &system_control);

		if (rc_a == 0 && rc_b == 0 && rc_s1 == 0 && rc_ref == 0 && rc_sys == 0) {
			daca = max30131_dec_dac_code(daca_raw[0], daca_raw[1]);
			dacb = max30131_dec_dac_code(dacb_raw[0], dacb_raw[1]);
			if (daca == expected.code_a && dacb == expected.code_b &&
			    s1_config1 == 0xC5U && reference_control == 0x01U &&
			    system_control == max30131_enc_system_control(false, false, false,
									 WP_CLK_40K)) {
				printk("POTENTIAL_AUDIT sample=%u target_mv=%d daca=%u dacb=%u "
				       "s1c1=%u ref=%u sys=%u\n", sample_count, e_mv, daca,
				       dacb, s1_config1, reference_control, system_control);
				return 0;
			}
		}
		k_msleep(1);
	}

	printk("POTENTIAL_FAULT sample=%u target_mv=%d expected_daca=%u "
	       "expected_dacb=%u actual_daca=%u actual_dacb=%u s1c1=%u ref=%u sys=%u\n",
	       sample_count, e_mv, expected.code_a, expected.code_b, daca, dacb,
	       s1_config1, reference_control, system_control);
	LOG_ERR("采集中电位寄存器异常:E=%d mV,DACA %u/%u,DACB %u/%u;停止本轮采集",
		e_mv, daca, expected.code_a, dacb, expected.code_b);
	log_status1("(电位审计故障)");
	return -EIO;
}

/*
 * 一次上电可执行多轮测量。测量间隙保持恒电位,不再通过 MCU/AFE 复位来
 * “开始”下一轮,避免电极每轮都经历失控再重新极化的伪阶跃。
 */
enum control_command {
	CONTROL_NONE = 0,
	CONTROL_START,
	CONTROL_STOP,
};

/* START/STOP 由 handle_command_line() 置位,poll 返回后清零。 */
static enum control_command pending_control;

/*
 * 从 RTT 下行取字符、拼成整行、交给 handle_command_line()。本函数**只管拼行**。
 *
 * 🔴 修一个静默注入漏洞(2026-08-10 发现,原代码存在):溢出时原来是
 *      `used = 0U;`  ← 丢掉已收部分,却**继续把后续字符往缓冲里塞**
 *    于是一条 >31 字符的命令(比如手抖多打了参数)会被切成两段,而**第二段会被
 *    当成一条完整命令执行**。若尾部恰好是 "START",就会真的启动一轮测量 ——
 *    没人下过这个命令,日志里也看不出为什么。
 *    现在改成:置 `overflow` 标志,一路丢弃**直到换行**,并回一条
 *    `CFG_REJECT reason=too_long`。丢弃是对的,报出来是必须的(公理 A3)。
 */
static enum control_command poll_control_command(void)
{
	static char command[AFE_CFG_LINE_MAX];
	static size_t used;
	static bool overflow;
	char ch;

	while (SEGGER_RTT_Read(0U, &ch, 1U) == 1U) {
		if (ch == '\r' || ch == '\n') {
			if (overflow) {
				printk("CFG_REJECT ep=%u ms=%lld reason=too_long "
				       "key=- a=%u b=%u raw=<discarded>\n",
				       cfg_epoch, (long long)k_uptime_get(),
				       (unsigned)used, AFE_CFG_LINE_MAX - 1);
				overflow = false;
				used = 0U;
				continue;
			}
			command[used] = '\0';
			used = 0U;
			handle_command_line(command);
			continue;
		}
		if (overflow) {
			used++;   /* 只为把真实长度报给上位机 */
			continue;
		}
		if (used + 1U < sizeof(command)) {
			command[used++] = ch;
		} else {
			overflow = true;
			used++;
		}
	}

	enum control_command c = pending_control;

	pending_control = CONTROL_NONE;
	return c;
}

/* ================================================================== */
/* idle(轮次之间)对电解池的处置 + 电极电压自监视                       */
/* ================================================================== */

/* 本设计 0x20 的固定部分:WE_DAC_MX=DACA、CE_DAC_MX=DACB、CP_EN=0、CHOP_EN=1。 */
static uint8_t s1_config1_byte(bool we_amp_en, bool ce_amp_en)
{
	const max30131_s1_config1_t c = {
		.we_amp_en = we_amp_en,
		.ce_amp_en = ce_amp_en,
		.we_dac_mx = MAX30131_DAC_MX_A,
		.ce_dac_mx = MAX30131_DAC_MX_B,
		.cp_en = false,
		.chop_en = true,
	};
	return max30131_enc_s1_config1(&c);
}

/*
 * 开/关恒电位放大器。**分两步写,次序照 CH Instruments 手册的电极接线告诫**:
 *   "You should connect the reference and counter electrodes first.
 *    When disconnecting, disconnect the working electrode first."
 * 断开:先关 WE、再关 CE。接通:先开 CE、再开 WE。
 * 我们是片内开关而非插拔,但同一道理 —— 不让 WE 在没有 CE 回路时单独带电。
 *
 * 🔴 成功后必须回写 `cfg_live.amps_on` —— 它是 plan 生成 0x20 字节的输入。
 *    不同步的话,下一次 SET(哪怕只改 FSR)会用错的 amps 位重写 0x20,
 *    在 idle 期间把放大器**静默打开**、给电解池加上电位。
 */
static int set_potentiostat_amps(bool on)
{
	int rc;

	if (on) {
		rc = max30131_spi_write_reg(MAX30131_REG_S1_CONFIG1,
					   s1_config1_byte(false, true));
		if (rc) {
			return rc;
		}
		rc = max30131_spi_write_reg(MAX30131_REG_S1_CONFIG1,
					    s1_config1_byte(true, true));
	} else {
		rc = max30131_spi_write_reg(MAX30131_REG_S1_CONFIG1,
					   s1_config1_byte(false, true));
		if (rc) {
			return rc;
		}
		rc = max30131_spi_write_reg(MAX30131_REG_S1_CONFIG1,
					    s1_config1_byte(false, false));
	}
	if (rc == 0) {
		cfg_live.amps_on = on;
	}
	return rc;
}

/*
 * 选/不选 Sensor 1 参与转换(0x24 bit0)。idle 探测期间必须**不选**——见下。
 * 同样要同步 cfg_live.sensor_selected(原 apply_range 写死 true 就是这个坑)。
 */
static int set_sensor_selected(bool selected)
{
	int rc = max30131_spi_write_reg(MAX30131_REG_S1_CONFIG5,
					max30131_enc_s1_config5(WP_CONV_TIME_CODE,
								selected));

	if (rc == 0) {
		cfg_live.sensor_selected = selected;
	}
	return rc;
}

/*
 * ══════════════════════════════════════════════════════════════════════
 * 电极电压的**连续**采集(idle 与测量期间一视同仁)
 * ══════════════════════════════════════════════════════════════════════
 * 由 System ADC 在 AUTO 模式下按 SYS_PERIOD 自主转换,与 Sensor ADC 的电流转换
 * **并行**,共用同一个 FIFO、靠 tag 区分(p143 明文)。所以不需要手动单次转换,
 * 也不需要在 idle/测量之间切换采集方式 —— 一条路径全覆盖。
 *
 * 🔴 为什么必须四路而不是只测 WE:E = V_WE − V_RE,而断开时 **RE 也在浮**,
 *    只有 WE 对芯片 GND 的电压推不出任何电化学量。CE 用于诊断(被拉到地 ⇒ 进了
 *    sensor-detect 类状态),WO 给出放大器输出的停靠点。
 * 🔴 为什么必须上报原始 12-bit code:整个电解池对芯片 GND 浮动时,电压可能跌破 0
 *    或超量程被削顶;只看 mV 看不出削顶,看 code 撞 0/4095 才看得出。
 */
static struct {
	int32_t mv[4];
	uint16_t code[4];
	bool got[4];
	bool ever_got;
	uint32_t dropped;    /* 因缺词被丢弃的组数(累计) */
	int64_t warned_at;
} cellv;

#define CELLV_WE 0
#define CELLV_RE 1
#define CELLV_CE 2
#define CELLV_WO 3

/*
 * OCP(开路电位)原语的状态。能力早就在跑了 —— IDLE_DISCONNECT + cellv 下
 * `CELL_V.e_mv` 本来就是开路电位;缺的是**名字、边界、收敛判据**。
 * 复用 CELL_V 行(加 `ocp=1` 字段)⇒ 上位机零新增解析器。
 */
static bool ocp_active;
static struct {
	int64_t begin_ms;
	int64_t last_ms;
	int32_t first_mv, last_mv, min_mv, max_mv;
	uint16_t we_code_min, we_code_max, re_code_min, re_code_max;
	uint32_t n;
	bool clipped;
} ocp;

static void ocp_observe(int32_t e_mv, uint16_t we_code, uint16_t re_code);
static void cellv_flush(void);

/*
 * 如果这个 FIFO 词是电极电压,收进 cellv 并返回 true(调用方应 continue);
 * 否则返回 false 交给电流路径。四路凑齐 WE+RE 就打一行。
 */
static bool collect_voltage_word(const max30131_fifo_word_t *w)
{
	int idx;

	if (!w->tag_is_8bit) {
		return false;
	}
	switch (w->tag) {
	case MAX30131_FIFO_TAG_S1_WE_V: idx = CELLV_WE; break;
	case MAX30131_FIFO_TAG_S1_RE_V: idx = CELLV_RE; break;
	case MAX30131_FIFO_TAG_S1_CE_V: idx = CELLV_CE; break;
	case MAX30131_FIFO_TAG_S1_WO_V: idx = CELLV_WO; break;
	default: return false;
	}

	/*
	 * 🔬 逐词 trace。加它的原因:2026-08-10 实测四路电位里**间歇性出现 0**,
	 * 连片内跟随器驱动的 WE 也会一采样 400mV、下一采样 0 —— 物理上不可能,
	 * 所以问题在数据通路而不在电极。而只看聚合后的 CELL_V 行永远看不出
	 * "哪个词是 0、来的顺序是什么、一个周期到底几个词"。
	 * 一个周期 4 路 ≈ 4 行/s,代价可接受;判死之后可关。
	 */
	if (WP_CELLV_TRACE) {
		printk("CELL_W ms=%lld tag=0x%02x code=%u auto=%d\n",
		       (long long)k_uptime_get(), w->tag, w->counts,
		       w->auto_mode ? 1 : 0);
	}
	/*
	 * 🔴 何时算"一组齐了"—— 原判据是"WE+RE 都到就打",错在**打得太早**:
	 * 实测到达次序是 CE → RE → WE → WO(0xD3/0xD2/0xD1/0xD0),
	 * 在 WE 到达时就打 ⇒ 本周期的 WO 还没来 ⇒ 打出的 WO 永远是**上一周期**的
	 * (或 -1)。而这一行的全部用途正是"同一时刻的四电极电位"。
	 *
	 * 改成**周期末 flush**,两个触发条件,与到达次序无关:
	 *   ① 某个 tag 重复出现 ⇒ 上一周期有词丢了、新周期已开始 ⇒ 先结算旧的
	 *   ② 四路到齐 ⇒ 正常结算
	 *
	 * 🔴 顺序纪律:条件①**必须在写入之前判**。我第一版把它写在
	 * `got[idx] = true` 之后 ⇒ `got[idx]` 恒为真 ⇒ 每个词都触发一次 flush,
	 * 于是每组永远缺 WO(实测 100% 的行 wo=-1)。写入前判、写入后结算。
	 */
	if (cellv.got[idx]) {
		cellv_flush();   /* 条件①:本词属于新周期,先把上一周期结算掉 */
	}

	cellv.code[idx] = w->counts;
	cellv.mv[idx] = max30131_sys_adc_mv(w->counts, max30131_ref_mv(WP_REF),
					    WP_SYSADC_SENSV_GAIN);
	cellv.got[idx] = true;
	cellv.ever_got = true;

	if (cellv.got[CELLV_WE] && cellv.got[CELLV_RE] &&
	    cellv.got[CELLV_CE] && cellv.got[CELLV_WO]) {
		cellv_flush();   /* 条件②:四路到齐 */
	}
	return true;
}

/* 把当前累积的一组四电极电位打成一行并清空。WE/RE 缺任一则只清不打(算不出 E)。 */
static void cellv_flush(void)
{
	/*
	 * 🔴 **只打完整的组**。缺任何一路就丢弃并计数,绝不用 -1 或残留值凑一行:
	 * 一行不完整的"四电极电位"在界面和 CSV 里跟真值长得一模一样,
	 * 而它会被当成真的电位去解释(2026-08-10 我自己就被 wo=-1 和 we=0 误导过)。
	 * 丢了多少必须可见 ⇒ 每行带 dropped= 累计值(公理 A3:沉默即故障)。
	 */
	if (!(cellv.got[CELLV_WE] && cellv.got[CELLV_RE] &&
	      cellv.got[CELLV_CE] && cellv.got[CELLV_WO])) {
		bool any = cellv.got[0] || cellv.got[1] || cellv.got[2] || cellv.got[3];

		if (any) {
			cellv.dropped++;
		}
		for (int i = 0; i < 4; i++) {
			cellv.got[i] = false;
		}
		return;
	}
	printk("CELL_V ms=%lld idle=%d we_mv=%d re_mv=%d ce_mv=%d wo_mv=%d "
	       "e_mv=%d we_code=%u re_code=%u ce_code=%u wo_code=%u ep=%u ocp=%d "
	       "dropped=%u\n",
	       (long long)k_uptime_get(), (int)WP_IDLE_MODE,
	       cellv.mv[CELLV_WE], cellv.mv[CELLV_RE],
	       cellv.mv[CELLV_CE], cellv.mv[CELLV_WO],
	       cellv.mv[CELLV_WE] - cellv.mv[CELLV_RE],
	       cellv.code[CELLV_WE], cellv.code[CELLV_RE],
	       cellv.code[CELLV_CE], cellv.code[CELLV_WO],
	       cfg_epoch, ocp_active ? 1 : 0, cellv.dropped);
	if (ocp_active) {
		ocp_observe(cellv.mv[CELLV_WE] - cellv.mv[CELLV_RE],
			    cellv.code[CELLV_WE], cellv.code[CELLV_RE]);
	}
	for (int i = 0; i < 4; i++) {
		cellv.got[i] = false;
	}
}

/*
 * idle 期间排空 FIFO —— 此时没有采集循环在跑,得有人来读,否则电压词只会
 * 在 FIFO 里滚掉(ro=1),上位机什么也看不到。
 */
static void drain_fifo_for_voltages(void)
{
	for (int n = 0; n < 12; n++) {
		uint8_t raw[3];
		max30131_fifo_word_t w;
		uint8_t st = 0;

		if (status1_read(&st) != 0) {
			return;
		}
		if (!(st & BIT(MAX30131_STATUS1_FIFO_DATA_RDY_Pos))) {
			return;
		}
		if (max30131_spi_read_burst(MAX30131_REG_FIFO_DATA, raw,
					    sizeof(raw)) != 0) {
			return;
		}
		if (max30131_fifo_unpack(raw, &w) != MAX30131_OK) {
			continue;
		}
		if (w.tag_is_8bit && w.tag == MAX30131_FIFO_TAG_EMPTY) {
			return;
		}
		(void)collect_voltage_word(&w);
	}
}

/*
 * 长时间一个电压词都没收到,最可能是 0x55 的 SYS_SELECT 位号猜错了
 * (datasheet 表格未能从文本层确认,按惯例取了 bit0)。大声报一次,不静默 ——
 * 这是个可检测、无损的假设。
 */
static void warn_if_no_voltages(void)
{
	int64_t now = k_uptime_get();

	if (cellv.ever_got || !WP_CELLV_ENABLE) {
		return;
	}
	/*
	 * 🔴 首次警告必须等够几个 SYS_PERIOD。原实现 warned_at==0 时立即喊,
	 * 于是开机 269ms 就报"至今未收到任何电极电压词"—— 那时 SYS_PERIOD(945ms)
	 * 连一个周期都没走完,**这条警告由构造决定必然误报**。
	 * 一个可检测的假设被自己的告警时机做成了永远为真的噪声,比不报更坏。
	 */
	if (now < 4 * (int64_t)drv_live.sysper_ms) {
		return;
	}
	if (cellv.warned_at != 0 && now - cellv.warned_at < 30000) {
		return;
	}
	cellv.warned_at = now;
	LOG_WRN("🔴 至今未收到任何电极电压词(tag 0xD1/0xD2)。"
		"首查 0x55 的 SYS_SELECT 位号假设(现取 bit%u,共 8 种可能);"
		"其次查 0x56=0x0F 与 SYS_PERIOD 是否够长",
		MAX30131_SYSADC_SYS_SELECT_Pos);
}

/*
 * 进入 idle。三种模式的差别收敛成两个正交开关:**sensor 选不选** 和 **放大器开不开**。
 *
 * 🔴 开了电极电压连采(WP_CELLV_ENABLE)时,**AUTO 必须保持运行** ——
 *    System ADC 的自主转换靠的就是它。所以此时 idle **不写 CONVERT=0**,
 *    改用「不选 sensor」来达到"电流不转换"的效果:
 *      STOP_CONV   : sensor 不选 + 放大器**开**  ⇒ 复现旧的那个坏中间态
 *      KEEP_BIASED : sensor 选   + 放大器开      ⇒ 转换从不停
 *      DISCONNECT  : sensor 不选 + 放大器**关**  ⇒ 真开路
 *    关掉连采时(A/B 用)才退回"直接写 CONVERT=0"的纯原始行为。
 */
static void enter_idle_state(void)
{
	if (!WP_CELLV_ENABLE && WP_IDLE_MODE != IDLE_KEEP_BIASED) {
		(void)max30131_spi_write_reg(MAX30131_REG_CONVERT_START,
					     max30131_enc_convert_start(false, false));
	} else if (WP_IDLE_MODE != IDLE_KEEP_BIASED) {
		(void)set_sensor_selected(false);
	}

	switch (WP_IDLE_MODE) {
	case IDLE_KEEP_BIASED:
		LOG_INF("idle=KEEP_BIASED:转换不停、电解池持续钳在 E=%d mV"
			"(边界跳变实测 −1.7nA;待机 7.3µA)", WP_STARTUP_E_MV);
		break;
	case IDLE_DISCONNECT:
		if (set_potentiostat_amps(false) != 0) {
			LOG_ERR("idle=DISCONNECT:关放大器失败");
		} else {
			LOG_INF("idle=DISCONNECT:WE/CE 放大器已关 ⇒ 真开路"
				"(残余仅引脚漏电 ±10pA typ)。与 CHI660 默认行为一致:"
				"实验结束即 cell off,下轮靠 Quiet Time 重新极化");
		}
		break;
	default:
		LOG_WRN("idle=STOP_CONV(仅作对照):电极会落到 p40 的固定 50nA 偏置通路,"
			"实测下一轮开头有 +385~474nA 还原冲击、~100s 恢复");
		break;
	}
}

/* 退出 idle:恢复放大器与 sensor 选择。电位与 Quiet Time 由调用方负责。 */
static void exit_idle_state(void)
{
	if (WP_IDLE_MODE == IDLE_DISCONNECT) {
		if (set_potentiostat_amps(true) != 0) {
			LOG_ERR("退出 idle:开放大器失败");
		}
		/* 放大器上电建立;DAC/REF 一直使能,不需要 12ms REF settle。 */
		k_msleep(20);
		LOG_INF("退出 idle:放大器已开(先 CE 后 WE)");
	}
	if (WP_IDLE_MODE != IDLE_KEEP_BIASED) {
		uint8_t s1c5 = 0U;

		(void)set_sensor_selected(true);
		/*
		 * 🔴 必须**重起** AUTO —— 光写 SELECT 位不生效(见 afe_restart_auto 注释)。
		 * 不重起的话第二轮及以后一个电流样本都拿不到,而且没有任何错误提示。
		 */
		if (afe_restart_auto() != 0) {
			LOG_ERR("退出 idle:重起 AUTO 失败 —— 本轮可能拿不到电流样本");
		}
		/* 回读确认 SELECT 真的是 1(器件是权威) */
		if (max30131_spi_read_reg(MAX30131_REG_S1_CONFIG5, &s1c5) == 0) {
			if ((s1c5 & 0x1U) == 0U) {
				LOG_ERR("🔴 退出 idle 后 0x24 SELECT 仍为 0(0x%02x)—— "
					"电流通道未选中,本轮不会有样本", s1c5);
			} else {
				LOG_INF("退出 idle:sensor 已选中并重起 AUTO(0x24=0x%02x)",
					s1c5);
			}
		}
	}
}

static void wait_for_start_command(void)
{
	printk("IT_READY target_mv=%d idle_mode=%d cellv=%d ep=%u\n", WP_E_MV,
	       (int)WP_IDLE_MODE, (int)WP_CELLV_ENABLE, cfg_epoch);
	while (1) {
		board_guards_feed();
		if (poll_control_command() == CONTROL_START) {
			return;
		}
		if (WP_CELLV_ENABLE) {
			/* idle 期间没有采集循环在读 FIFO,得有人来排 —— 否则电压词滚掉。 */
			drain_fifo_for_voltages();
			warn_if_no_voltages();
		}
		/* idle 期间也要有 STATUS1 心跳 —— 否则"设备在待命"与"固件死了"同形 */
		afe_status_poll("idle", false);
		k_msleep(20);
	}
}

/* ================================================================== */
/* 🎯 双档增益标定(datasheet p41)                                      */
/* ================================================================== */
/*
 * 六个量程档里**只有 500nA 档出厂校准到 ±1%**(其余档规格表只给 typ、不给
 * min/max)。datasheet 给的反算法:
 *
 *   步1  IOFFSET      = 500nA × ADC_OUT(500nA) / 2¹⁶      ← 借已校准档测 offset 真值
 *   步2  FSR(目标档)  = IOFFSET × 2¹⁶ / ADC_OUT(目标档)   ← 反推目标档真实满量程
 *
 * 🔴 标定用 **SEL=6(40nA typ)**,不是工作档位。被测 offset 越大,
 *   ADC 绝对 offset 误差占的相对比例越小。SEL6 规格上限 46nA,
 *   可安全装入 50nA 目标档和 500nA 参考档。
 */
#define CAL_OFFSET_SEL MAX30131_OFFSET_SEL6_40NA
#define CAL_REF_FSR    MAX30131_FSR_500NA

/* 切 FSR 与 offset 档(改 S1_CONFIG4 的 [7:5] 与 [2:0]) */
static int set_fsr_and_offset(max30131_fsr_t fsr, max30131_offset_sel_t off)
{
	return max30131_spi_write_reg(0x23U, max30131_enc_s1_config4(fsr, off));
}

/*
 * 方案 C 的落点:在线切换量程/偏置档。
 *
 * 只写两个寄存器:
 *   0x23 S1_CONFIG4 = FSR + OFFSET_SEL
 *   0x24 S1_CONFIG5 = CONV_TIME  ← **必须跟着改**:FSR 码 ≤3 走慢钟、>3 走 4× 快钟,
 *        同一个 CONV_TIME 码两组的积分时间差 4 倍。不跟着改就会 conv > SENS_PERIOD
 *        ⇒ INVALID_CFG、AFE 不出数(2026-08-09 已因写死 CONV_TIME 栽过一次)。
 *
 * 🔴 **绝不触碰 DACA/DACB** —— 那是极化电位。整个切档过程恒电位环保持闭合、
 *    电极一直在设定电位上,所以不产生新的初始瞬态。这正是方案 C 的全部意义。
 *
 * ⚠️ 未知:AUTO=1 运行中写 0x23/0x24 是否立即生效。datasheet 明确 SENSOR_CAL 和
 *    手动转换在 AUTO=1 下被静默忽略,但没说这两个寄存器。**必须实测**:切档后
 *    counts 应跳到新档位预期值。不生效的话会安静地继续用旧档换算 ⇒ 见 RANGE_APPLIED
 *    行与实际 counts 是否自洽。
 *
 * 切换前后 `counts` 不可比(LSB 与零点都变了);`fa` 仍是物理电流,可比。
 * RANGE_APPLIED 行就是给上位机/事后分析定位切换点用的。
 *
 * 🔴 2026-08-10:`apply_range()` 这个专用路径已**并入统一 SET 路径**。
 *    两条路径管同一件事必然分叉 —— 原 apply_range 就把 `SELECT` 写死成 true,
 *    于是 idle(sensor 已 deselect)期间收到 RANGE 会静默把 sensor 重新选中、
 *    让电流转换在开路态跑起来。现在写序与 SELECT 都由 lib/afe_cfg 的 plan 负责,
 *    但 `RANGE_APPLIED` / `RANGE_REJECT` 两行**逐字保留**(GUI 依赖它们)。
 */

/* 遗留兼容行:只要 FSR/offset 变了就打,不论命令是 RANGE 还是 SET。 */
static void emit_range_applied(void)
{
	printk("RANGE_APPLIED fsr_code=%d offset_sel=%d fsr_pa=%d off_pa=%d "
	       "bits=%u lsb_eff_fa=%d sat_margin=%u red_max_pa=%d ox_max_pa=%d\n",
	       (int)cfg_live.fsr, (int)cfg_live.off, drv_live.fsr_pa,
	       drv_live.off_pa, drv_live.bits, drv_live.lsb_eff_fa,
	       drv_live.sat_margin, drv_live.red_max_pa, drv_live.ox_max_pa);
}

/* ================================================================== */
/* STATUS1 监视:搭 1Hz 电位审计的便车                                  */
/* ================================================================== */
/*
 * 🔴 为什么不另起一个定时器:`audit_polarization()` 已经每 1000ms 读 5 个寄存器,
 *    加第六个读成本≈0,而**新的周期性活动本身就是噪声源** —— 项目正在担心
 *    System ADC 每 945ms 活动 68ms 会不会往 pA 级电流里注入 1.06Hz 谱线,
 *    不该再自己添一个节奏。
 *
 * 上报规则:状态字变化即报 + **30s 心跳**。心跳不是冗余 —— 没有它时,
 * "一切正常"与"这段监视代码从没跑过"在日志里完全同形(公理 A3)。
 */
static uint8_t status1_last = 0xFFU;
static int64_t status1_reported_ms;
static uint8_t invalid_cfg_streak;
static bool afe_fault_invalid;   /* 采集循环据此中止本轮 */
static bool afe_fault_vdd;

#define AFE_STATUS_HEARTBEAT_MS 30000

static void afe_status_poll(const char *why, bool force)
{
	uint8_t st = 0;
	int64_t now = k_uptime_get();
	bool invalid, vdd_oor;

	if (status1_read(&st) != 0) {
		printk("AFE_STATUS ep=%u ms=%lld status1=read_fail why=%s\n",
		       cfg_epoch, (long long)now, why);
		return;
	}
	invalid = (st & BIT(MAX30131_STATUS1_INVALID_CFG_Pos)) != 0U;
	/*
	 * 🔴 两个物理事件位都**读清**,而且可能已被 20ms 的 FIFO 轮询吃掉
	 * ⇒ 判据取「本次读到的 | sticky 累积的」,报完再清 sticky。
	 * 🔴 PWR_RDY 的语义与名字相反:1 = VDD 曾掉到 1.55V UVLO 以下(掉压),
	 *    0 = 正常。首次实测 STATUS1=0x00 时我按字面把它当"电源未就绪"报了故障,
	 *    是**读反了 datasheet**(p82 已核,regs.h 头部记了原文)。
	 */
	uint8_t sticky = status1_sticky;

	vdd_oor = ((st | sticky) & BIT(MAX30131_STATUS1_VDD_OOR_Pos)) != 0U;
	bool brownout = ((st | sticky) & BIT(MAX30131_STATUS1_PWR_RDY_Pos)) != 0U;

	/*
	 * ⚠️ INVALID_CFG 是 latched 还是 live **未定**(datasheet 未明说)。
	 * 判据刻意做成不依赖答案:连续两次才升级为故障。若它是 latched,
	 * 第一次写坏配置后就会一直亮 —— 那也应该中止,所以两种语义下行为都正确。
	 */
	if (invalid) {
		if (invalid_cfg_streak < 255U) {
			invalid_cfg_streak++;
		}
	} else {
		invalid_cfg_streak = 0U;
	}
	if (invalid_cfg_streak >= 2U && acquiring) {
		afe_fault_invalid = true;
	}
	if (vdd_oor && acquiring) {
		afe_fault_vdd = true;
	}
	/*
	 * 掉压不中止本轮(器件已恢复,后续读数仍是测量),但**必须打 tainted**:
	 * 掉压跨越的那段时间里基准与 offset 源都不在规格内,那些点不能进标定曲线。
	 */
	if (brownout) {
		if (acquiring) {
			run_tainted = true;
			printk("IT_TAINTED ep=%u reason=brownout\n", cfg_epoch);
		}
		LOG_ERR("🔴 PWR_RDY 置位 —— VDD 曾跌破 1.55V UVLO 掉压"
			"(不是「电源未就绪」,名字与语义相反,见 regs.h)。"
			"查 CR2032 内阻/接触与去耦");
	}

	if (!force && st == status1_last && sticky == 0U &&
	    now - status1_reported_ms < AFE_STATUS_HEARTBEAT_MS) {
		return;
	}
	status1_last = st;
	status1_reported_ms = now;
	/* `brownout=` 取代原来那个会读反的 `pwr_rdy=`;sticky 一并上报,便于事后区分
	 * "这一刻置位"与"这一秒里发生过" */
	printk("AFE_STATUS ep=%u ms=%lld status1=0x%02x sticky=0x%02x invalid_cfg=%d "
	       "vdd_oor=%d brownout=%d a_full=%d data_rdy=%d streak=%u acquiring=%d "
	       "why=%s\n",
	       cfg_epoch, (long long)now, st, sticky, invalid ? 1 : 0, vdd_oor ? 1 : 0,
	       brownout ? 1 : 0,
	       (st >> MAX30131_STATUS1_A_FULL_Pos) & 1,
	       (st >> MAX30131_STATUS1_FIFO_DATA_RDY_Pos) & 1,
	       invalid_cfg_streak, acquiring ? 1 : 0, why);
	status1_sticky = 0U;
}

/* ================================================================== */
/* 配置提交:执行 plan → 回读 → 确认 → 不一致则回滚                     */
/* ================================================================== */
/*
 * 四道闸门(公理 A4「器件是权威」):
 *   ① validate 拒非法终点   ② plan 保证每个中间态合法(全组合枚举单测背书)
 *   ③ 每写必回读             ④ 写完复核 STATUS1,芯片不同意就回滚 + 报 model_mismatch
 *
 * 次序:先写 ADC 侧寄存器,**最后**才动 DAC。理由:电位一变电解池就开始响应,
 * 此时 ADC 侧应当已经处在新配置上,否则那一小段数据既不属于旧配置也不属于新配置。
 */
static int exec_plan(const afe_plan_t *plan, uint32_t ep, const char *tag)
{
	int bad = 0;

	for (uint8_t i = 0; i < plan->n; i++) {
		uint8_t before = 0U, after = 0U;

		(void)max30131_spi_read_reg(plan->w[i].addr, &before);
		if (max30131_spi_write_reg(plan->w[i].addr, plan->w[i].val) != 0) {
			printk("CFG_FAULT ep=%u cause=spi_write addr=0x%02x tag=%s\n",
			       ep, plan->w[i].addr, tag);
			return -EIO;
		}
		if (max30131_spi_read_reg(plan->w[i].addr, &after) != 0) {
			printk("CFG_FAULT ep=%u cause=spi_read addr=0x%02x tag=%s\n",
			       ep, plan->w[i].addr, tag);
			return -EIO;
		}
		if (afe_cfg_fmt_reg(ep, (uint8_t)(i + 1U), plan->n, plan->w[i].addr,
				    before, plan->w[i].val, after, audit_line,
				    sizeof(audit_line)) > 0) {
			printk("%s\n", audit_line);
		}
		if (after != plan->w[i].val) {
			bad++;
		}
	}
	return bad == 0 ? 0 : -EIO;
}

static bool potential_differs(const afe_cfg_t *a, const afe_cfg_t *b)
{
	return a->e_mv != b->e_mv || a->vwe_mv != b->vwe_mv;
}

static int afe_cfg_commit(const afe_cmd_t *cmd, const char *src)
{
	afe_cfg_t cand = cmd->cfg;
	afe_derived_t dcand;
	afe_cfg_t prev = cfg_live;
	afe_derived_t dprev = drv_live;
	afe_plan_t plan;
	afe_reject_t why;
	uint32_t ep;
	bool need_dac, range_changed, idle_changed;

	/* 运行态不由命令设:它们由 idle/采集流程拥有 */
	cand.sensor_selected = cfg_live.sensor_selected;
	cand.amps_on = cfg_live.amps_on;

	afe_cfg_derive(&cand, &dcand);
	if (!afe_cfg_validate(&cand, &dcand, acquiring, cmd->forced, &why)) {
		if (afe_cfg_fmt_reject(cfg_epoch, k_uptime_get(), &why, src,
				       audit_line, sizeof(audit_line)) > 0) {
			printk("%s\n", audit_line);
		}
		if (cmd->legacy_range) {
			printk("RANGE_REJECT reason=%s\n", afe_rej_name(why.code));
		}
		return -EINVAL;
	}

	afe_cfg_plan(&prev, &dprev, &cand, &dcand, &plan);

	/*
	 * 「采集中不许扰动电解池」—— 这条只有 plan 判得了(要比前后两个配置)。
	 * 带 FORCE 放行,但给本轮打 tainted:那段数据不能进标定曲线。
	 */
	if (acquiring && plan.perturbs_cell && !cmd->forced) {
		afe_reject_t p = { .code = AFE_REJ_PERTURB_DURING_RUN,
				   .key = "", .a = 0, .b = 0 };

		if (afe_cfg_fmt_reject(cfg_epoch, k_uptime_get(), &p, src,
				       audit_line, sizeof(audit_line)) > 0) {
			printk("%s\n", audit_line);
		}
		return -EBUSY;
	}

	need_dac = potential_differs(&prev, &cand);
	range_changed = prev.fsr != cand.fsr || prev.off != cand.off;
	idle_changed = prev.idle != cand.idle;

	if (plan.n == 0U && !need_dac && !idle_changed) {
		/* 什么都没变 ⇒ 不 ep++、不写寄存器。GET 的语义正是这一支。 */
		printk("CFG_NOOP ep=%u src=%s skipped=%u\n", cfg_epoch, src,
		       plan.skipped);
		return 0;
	}

	ep = ++cfg_epoch;
	if (afe_cfg_fmt_applied(ep, k_uptime_get(), src, cmd->n_keys, cmd->forced,
				&plan, &prev, &cand, audit_line, sizeof(audit_line)) > 0) {
		printk("%s\n", audit_line);
	}
	if (afe_cfg_fmt_derived(ep, &cand, &dcand, audit_line, sizeof(audit_line)) > 0) {
		printk("%s\n", audit_line);
	}

	/* System ADC 增益寄存器不在 plan 里(它是常量),开启连采时补写一次 */
	if (cand.cellv && !prev.cellv) {
		(void)max30131_spi_write_reg(MAX30131_REG_SYS_ADC_SETUP,
			max30131_enc_sys_adc_setup(WP_SYSADC_SENSV_GAIN));
	}

	if (exec_plan(&plan, ep, "apply") != 0) {
		goto rollback;
	}

	/* 🔴 cfg_live 整体赋值 —— 中间态不存在于本变量里(公理 A2) */
	cfg_live = cand;
	drv_live = dcand;

	if (need_dac && set_polarization(cfg_live.e_mv) != 0) {
		printk("CFG_FAULT ep=%u cause=dac_write\n", ep);
		goto rollback;
	}
	if (idle_changed && !acquiring) {
		/* idle 语义变了而现在正处于 idle ⇒ 立刻按新语义重置电解池处置 */
		enter_idle_state();
	}

	afe_status_poll("post_commit", true);
	{
		uint8_t st = status1_last;

		printk("CFG_CONFIRMED ep=%u status1=0x%02x invalid_cfg=%d vdd_oor=%d "
		       "pwr_rdy=%d nregs=%u skipped=%u\n", ep, st,
		       (st >> MAX30131_STATUS1_INVALID_CFG_Pos) & 1,
		       (st >> MAX30131_STATUS1_VDD_OOR_Pos) & 1,
		       (st >> MAX30131_STATUS1_PWR_RDY_Pos) & 1,
		       plan.n, plan.skipped);
		if (st & BIT(MAX30131_STATUS1_INVALID_CFG_Pos)) {
			/*
			 * 校验器说合法、器件说不合法 ⇒ **我们的模型错了**,不是用户错了。
			 * 这是本设计里唯一能自动发现"lib 与 datasheet 不一致"的地方。
			 */
			printk("CFG_FAULT ep=%u cause=model_mismatch status1=0x%02x\n",
			       ep, st);
			goto rollback;
		}
	}
	if (range_changed) {
		emit_range_applied();
	}
	if (plan.perturbs_cell && acquiring) {
		run_tainted = true;
		printk("IT_TAINTED ep=%u reason=perturb_during_run\n", ep);
	}
	return 0;

rollback:
	{
		afe_plan_t back;

		afe_cfg_plan(&cand, &dcand, &prev, &dprev, &back);
		printk("CFG_ROLLBACK ep=%u nregs=%u\n", ep, back.n);
		(void)exec_plan(&back, ep, "rollback");
		cfg_live = prev;
		drv_live = dprev;
		if (need_dac) {
			(void)set_polarization(cfg_live.e_mv);
		}
		afe_status_poll("post_rollback", true);
	}
	return -EIO;
}

/* ================================================================== */
/* OCP(开路电位)                                                      */
/* ================================================================== */
static void ocp_observe(int32_t e_mv, uint16_t we_code, uint16_t re_code)
{
	if (ocp.n == 0U) {
		ocp.first_mv = e_mv;
		ocp.min_mv = e_mv;
		ocp.max_mv = e_mv;
		ocp.we_code_min = we_code;
		ocp.we_code_max = we_code;
		ocp.re_code_min = re_code;
		ocp.re_code_max = re_code;
	}
	ocp.last_mv = e_mv;
	ocp.last_ms = k_uptime_get();
	if (e_mv < ocp.min_mv) {
		ocp.min_mv = e_mv;
	}
	if (e_mv > ocp.max_mv) {
		ocp.max_mv = e_mv;
	}
	if (we_code < ocp.we_code_min) {
		ocp.we_code_min = we_code;
	}
	if (we_code > ocp.we_code_max) {
		ocp.we_code_max = we_code;
	}
	if (re_code < ocp.re_code_min) {
		ocp.re_code_min = re_code;
	}
	if (re_code > ocp.re_code_max) {
		ocp.re_code_max = re_code;
	}
	/* 🔴 削顶必须看原始 code:整池对芯片 GND 浮动时会撞 0 或 4095 */
	if (we_code == 0U || we_code >= 4095U || re_code == 0U || re_code >= 4095U) {
		ocp.clipped = true;
	}
	ocp.n++;
}

#define OCP_DEFAULT_MS 10000
#define OCP_MIN_MS 1000
#define OCP_MAX_MS 120000
#define OCP_SETTLED_UV_PER_S 200   /* |斜率| 低于此值才敢说收敛 */

static void ocp_run(int32_t window_ms, bool forced)
{
	bool amps_before = cfg_live.amps_on;
	bool sel_before = cfg_live.sensor_selected;
	int32_t settle_ms;
	int64_t elapsed;
	int64_t slope = 0;
	bool settled;

	if (window_ms <= 0) {
		window_ms = OCP_DEFAULT_MS;
	}
	if (window_ms < OCP_MIN_MS || window_ms > OCP_MAX_MS) {
		printk("OCP_REJECT ep=%u ms=%lld reason=arg a=%d hint=%d..%d\n",
		       cfg_epoch, (long long)k_uptime_get(), window_ms,
		       OCP_MIN_MS, OCP_MAX_MS);
		return;
	}
	/*
	 * 冲突处置 = **拒绝,不加锁**。关放大器会毁掉正在跑的轮次;阻塞锁只是把
	 * 破坏推迟,静默执行则是数据污染。三种拒因都给 hint,让人知道下一步该做什么。
	 */
	if (acquiring && !forced) {
		printk("OCP_REJECT ep=%u ms=%lld reason=busy hint=STOP_first_or_FORCE\n",
		       cfg_epoch, (long long)k_uptime_get());
		return;
	}
	if (!cfg_live.cellv) {
		/* 🔴 刻意不自动改配置 —— 静默改配置比拒绝更坏 */
		printk("OCP_REJECT ep=%u ms=%lld reason=cellv_off "
		       "hint=SET_cellv=1_first\n", cfg_epoch,
		       (long long)k_uptime_get());
		return;
	}
	if (cfg_live.idle == AFE_IDLE_KEEP_BIASED) {
		printk("OCP_REJECT ep=%u ms=%lld reason=idle_keep_biased "
		       "hint=definition_conflict_SET_idle=2\n", cfg_epoch,
		       (long long)k_uptime_get());
		return;
	}
	if (acquiring) {
		run_tainted = true;
		printk("IT_TAINTED ep=%u reason=ocp_during_run\n", cfg_epoch);
	}

	settle_ms = 2 * drv_live.sysper_ms;
	memset(&ocp, 0, sizeof(ocp));
	ocp.begin_ms = k_uptime_get();
	printk("OCP_BEGIN ep=%u ms=%lld window_ms=%d sysper_ms=%d settle_ms=%d "
	       "amps0=%d sel0=%d\n", cfg_epoch, (long long)ocp.begin_ms,
	       window_ms, drv_live.sysper_ms, settle_ms, amps_before ? 1 : 0,
	       sel_before ? 1 : 0);

	(void)set_sensor_selected(false);
	(void)set_potentiostat_amps(false);

	/* 建立期:数据丢掉不看 */
	for (int32_t t = 0; t < settle_ms; t += POLL_INTERVAL_MS) {
		board_guards_feed();
		drain_fifo_for_voltages();
		k_msleep(POLL_INTERVAL_MS);
	}
	memset(&ocp, 0, sizeof(ocp));
	ocp.begin_ms = k_uptime_get();
	ocp_active = true;
	for (int32_t t = 0; t < window_ms; t += POLL_INTERVAL_MS) {
		board_guards_feed();
		drain_fifo_for_voltages();
		k_msleep(POLL_INTERVAL_MS);
	}
	ocp_active = false;

	elapsed = ocp.last_ms - ocp.begin_ms;
	if (elapsed > 0) {
		slope = ((int64_t)(ocp.last_mv - ocp.first_mv) * 1000000) / elapsed;
	}
	/*
	 * 🔴 `settled` 默认 0 —— **未证明收敛就是未收敛**。
	 * 有效性前提(写进 07 文档):若 OCP 单调漂向 VDD 或 0 轨而不是停在中间某个
	 * 定值,那是 System ADC 输入在充电,**不是电化学 OCP**。we_code_min/max 与
	 * clipped 就是这条判据的证据,所以必须一起上报。
	 */
	settled = ocp.n >= 5U && !ocp.clipped &&
		  slope <= OCP_SETTLED_UV_PER_S && slope >= -OCP_SETTLED_UV_PER_S;
	printk("OCP_DONE ep=%u ms=%lld n=%u elapsed_ms=%lld first_mv=%d last_mv=%d "
	       "min_mv=%d max_mv=%d slope_uv_per_s=%lld settled=%d clipped=%d "
	       "we_code_min=%u we_code_max=%u re_code_min=%u re_code_max=%u\n",
	       cfg_epoch, (long long)k_uptime_get(), ocp.n, (long long)elapsed,
	       ocp.first_mv, ocp.last_mv, ocp.min_mv, ocp.max_mv,
	       (long long)slope, settled ? 1 : 0, ocp.clipped ? 1 : 0,
	       ocp.we_code_min, ocp.we_code_max, ocp.re_code_min, ocp.re_code_max);

	/* 精确恢复进入态并回读 —— OCP 不该留下任何副作用 */
	if (amps_before) {
		(void)set_potentiostat_amps(true);
		k_msleep(20);
		(void)set_polarization(cfg_live.e_mv);
	}
	(void)set_sensor_selected(sel_before);
	{
		uint8_t s1 = 0U, s5 = 0U;

		(void)max30131_spi_read_reg(MAX30131_REG_S1_CONFIG1, &s1);
		(void)max30131_spi_read_reg(MAX30131_REG_S1_CONFIG5, &s5);
		printk("OCP_RESTORED ep=%u ms=%lld amps=%d sel=%d e_mv=%d "
		       "s1c1=0x%02x s1c5=0x%02x ok=%d\n", cfg_epoch,
		       (long long)k_uptime_get(), cfg_live.amps_on ? 1 : 0,
		       cfg_live.sensor_selected ? 1 : 0, cfg_live.e_mv, s1, s5,
		       (cfg_live.amps_on == amps_before &&
			cfg_live.sensor_selected == sel_before) ? 1 : 0);
	}
	afe_status_poll("post_ocp", true);
}

/* ================================================================== */
/* 命令分派                                                            */
/* ================================================================== */
/* GET:幂等重放当前状态。**不 ep++、不写任何寄存器** —— 上行丢行时靠它补齐。 */
static void replay_state(const char *src)
{
	afe_plan_t empty = { 0 };

	if (afe_cfg_fmt_applied(cfg_epoch, k_uptime_get(), src, 0, false, &empty,
				&cfg_live, &cfg_live, audit_line, sizeof(audit_line)) > 0) {
		printk("%s\n", audit_line);
	}
	if (afe_cfg_fmt_derived(cfg_epoch, &cfg_live, &drv_live, audit_line,
				sizeof(audit_line)) > 0) {
		printk("%s\n", audit_line);
	}
	afe_status_poll(src, true);
}

static void handle_command_line(const char *line)
{
	afe_cmd_t cmd;
	afe_reject_t why;

	if (!afe_cfg_parse(line, &cfg_live, &cmd, &why)) {
		if (afe_cfg_fmt_reject(cfg_epoch, k_uptime_get(), &why, line,
				       audit_line, sizeof(audit_line)) > 0) {
			printk("%s\n", audit_line);
		}
		/* 遗留兼容:RANGE 的拒因也走老行,GUI 才看得见 */
		if (strncmp(line, "RANGE", 5) == 0) {
			printk("RANGE_REJECT reason=%s\n", afe_rej_name(why.code));
		}
		return;
	}

	switch (cmd.verb) {
	case AFE_VERB_NONE:
		break;
	case AFE_VERB_START:
		pending_control = CONTROL_START;
		break;
	case AFE_VERB_STOP:
		pending_control = CONTROL_STOP;
		break;
	case AFE_VERB_GET:
		replay_state("get");
		break;
	case AFE_VERB_STATUS:
		afe_status_poll("status_cmd", true);
		break;
	case AFE_VERB_SET:
		(void)afe_cfg_commit(&cmd, cmd.legacy_range ? "range" : "cmd");
		break;
	case AFE_VERB_PEEK: {
		uint8_t v = 0U;
		int rc;

		if (cmd.arg0 < 0 || cmd.arg0 > 0xFF) {
			printk("CFG_REJECT ep=%u reason=arg key=addr a=%d b=255\n",
			       cfg_epoch, cmd.arg0);
			break;
		}
		rc = max30131_spi_read_reg((uint8_t)cmd.arg0, &v);
		printk("REG_PEEK ep=%u addr=0x%02x val=0x%02x rc=%d\n", cfg_epoch,
		       (unsigned)cmd.arg0, v, rc);
		break;
	}
	case AFE_VERB_POKE: {
		uint8_t before = 0U, after = 0U;

		if (cmd.arg0 < 0 || cmd.arg0 > 0xFF || cmd.arg1 < 0 ||
		    cmd.arg1 > 0xFF) {
			printk("CFG_REJECT ep=%u reason=arg key=poke a=%d b=%d\n",
			       cfg_epoch, cmd.arg0, cmd.arg1);
			break;
		}
		/*
		 * 🔴 POKE 绕过全部校验与 cfg_live 记账 ⇒ 之后 cfg_live 与器件可能不符。
		 * 所以强制要 FORCE,并明确标注 desync：任何用 POKE 之后的数据都必须
		 * 先发一次 GET 重建认知,否则换算口径无法保证。
		 */
		if (!cmd.forced) {
			printk("CFG_REJECT ep=%u reason=arg key=poke_needs_FORCE "
			       "a=%d b=%d\n", cfg_epoch, cmd.arg0, cmd.arg1);
			break;
		}
		(void)max30131_spi_read_reg((uint8_t)cmd.arg0, &before);
		(void)max30131_spi_write_reg((uint8_t)cmd.arg0, (uint8_t)cmd.arg1);
		(void)max30131_spi_read_reg((uint8_t)cmd.arg0, &after);
		printk("REG_POKE ep=%u addr=0x%02x before=0x%02x wrote=0x%02x "
		       "readback=0x%02x ok=%d desync=1\n", cfg_epoch,
		       (unsigned)cmd.arg0, before, (unsigned)cmd.arg1, after,
		       after == (uint8_t)cmd.arg1 ? 1 : 0);
		afe_status_poll("post_poke", true);
		break;
	}
	case AFE_VERB_OCP:
		ocp_run(cmd.arg0, cmd.forced);
		break;
	default:
		break;
	}
}

/* 切 IOFFSET_CONV(0=信号+offset,1=仅 offset) */
static int set_ioffset_conv_period(uint8_t mode, uint8_t period_code)
{
	return max30131_spi_write_reg(MAX30131_REG_CONVERT_SETUP1,
					      max30131_enc_convert_setup1(false, mode, false,
								  period_code));
}

static int set_ioffset_conv(uint8_t mode)
{
	return set_ioffset_conv_period(mode, WP_SENS_PERIOD_CODE);
}

/*
 * 复位正在 AUTO 转换的 AFE 后,第一个 offset-only FIFO 字偶尔会是
 * counts=1。标定档的 offset 规格给出了可预知的 counts 窗口;超窗的
 * 样本不能参与反算,必须丢弃并重做转换。窗口外扩 5% 覆盖 FSR 偏差。
 */
static int calibration_convert(max30131_fsr_t fsr, max30131_offset_sel_t offset,
			       uint16_t *counts_out, uint8_t *tag_out, const char *step)
{
	int32_t offset_lo_pa = 0, offset_hi_pa = 0;
	int32_t fsr_pa = max30131_fsr_pa(fsr);

	max30131_offset_range_pa(offset, fsr, &offset_lo_pa, &offset_hi_pa);
	int32_t counts_lo = (int32_t)((int64_t)offset_lo_pa * 65536 / fsr_pa);
	int32_t counts_hi = (int32_t)((int64_t)offset_hi_pa * 65536 / fsr_pa);

	counts_lo = counts_lo * 95 / 100;
	counts_hi = counts_hi * 105 / 100;

	for (int attempt = 1; attempt <= 3; attempt++) {
		uint16_t counts = 0;
		uint8_t tag = 0;
		int rc = manual_convert_once(&counts, &tag);

		if (rc == 0 && counts >= counts_lo && counts <= counts_hi) {
			*counts_out = counts;
			*tag_out = tag;
			return 0;
		}
		LOG_WRN("标定%s第 %d 次转换作废:counts=%u,期望 [%d,%d],rc=%d",
			step, attempt, counts, counts_lo, counts_hi, rc);
		k_msleep(50);
	}
	return -ERANGE;
}

static void afe_calibrate(void)
{
	uint16_t adc_ref = 0, adc_tgt = 0, adc_base = 0;
	uint8_t tag;

	LOG_INF("──── 双档增益标定(参考档 500nA / 目标档 %d nA)────",
		max30131_fsr_pa(WP_FSR) / 1000);

	/* 500nA 参考档属于慢钟组;用 16-bit/1.882s 规避快速档切换后的假 DATA_RDY。 */
	if (max30131_spi_write_reg(MAX30131_REG_S1_CONFIG5,
					 max30131_enc_s1_config5(CAL_CONV_TIME_CODE, true)) != 0 ||
	    set_ioffset_conv_period(1U, CAL_SENS_PERIOD_CODE) != 0) { /* offset-only */
		LOG_ERR("标定:切 IOFFSET_CONV=1 失败");
		return;
	}

	/* 步1:参考档(500nA)+ 大 offset(40nA)→ offset 真值 */
	if (set_fsr_and_offset(CAL_REF_FSR, CAL_OFFSET_SEL) != 0 ||
	    calibration_convert(CAL_REF_FSR, CAL_OFFSET_SEL, &adc_ref, &tag, "步1") != 0) {
		LOG_ERR("标定步1失败(500nA 档 offset-only)");
		goto restore;
	}
	int32_t ioffset_pa = max30131_cal_ioffset_pa(adc_ref,
						     max30131_fsr_pa(CAL_REF_FSR));

	/* 步2:目标档同一 offset → 反推该档 FSR 真值 */
	if (set_fsr_and_offset(WP_FSR, CAL_OFFSET_SEL) != 0 ||
	    calibration_convert(WP_FSR, CAL_OFFSET_SEL, &adc_tgt, &tag, "步2") != 0) {
		LOG_ERR("标定步2失败(%d nA 档 offset-only)",
			max30131_fsr_pa(WP_FSR) / 1000);
		goto restore;
	}
	int32_t fsr_true = max30131_cal_fsr_pa(ioffset_pa, adc_tgt);

	int32_t fsr_nom = max30131_fsr_pa(WP_FSR);
	int32_t err_ppt = (int32_t)(((int64_t)fsr_true - fsr_nom) * 1000 / fsr_nom);

	LOG_INF("  步1 500nA 档 counts=%5u → offset 真值 %d pA(SEL=6 规格 34000–46000)",
		adc_ref, ioffset_pa);
	LOG_INF("  步2 %3d nA 档 counts=%5u → FSR 真值 %d pA(标称 %d,偏差 %d.%d%%)",
		max30131_fsr_pa(WP_FSR) / 1000, adc_tgt, fsr_true, fsr_nom, err_ppt / 10,
		(err_ppt < 0 ? -err_ppt : err_ppt) % 10);

	/* 合理性闸门:offset 真值必须落在该档规格窗口内,否则标定不可信 */
	int32_t lo = 0, hi = 0;

	max30131_offset_range_pa(CAL_OFFSET_SEL, CAL_REF_FSR, &lo, &hi);
	if (ioffset_pa < lo || ioffset_pa > hi) {
		LOG_ERR("🔴 标定 offset 真值 %d pA 超出规格窗口 [%d,%d] —— 标定作废,"
			"退回标称值。查:是否真 offset-only?量程切换是否生效?",
			ioffset_pa, lo, hi);
		goto restore;
	}
	/* FSR 真值偏离标称超 ±10% 也不信(规格增益误差仅 ±2%) */
	if (err_ppt > 100 || err_ppt < -100) {
		LOG_ERR("🔴 FSR 真值偏离标称 %d.%d%% > 10%%,超出 ±2%% 增益规格太多"
			" —— 标定作废", err_ppt / 10,
			(err_ppt < 0 ? -err_ppt : err_ppt) % 10);
		goto restore;
	}

	fsr_cal_pa = fsr_true;

	/* 步3:回到**工作 offset 档**,测同档 offset-only 基线(差分换算的减数) */
	if (set_fsr_and_offset(WP_FSR, WP_OFFSET_SEL) != 0 ||
	    calibration_convert(WP_FSR, WP_OFFSET_SEL, &adc_base, &tag, "步3") != 0) {
		LOG_ERR("标定步3失败(工作档 offset-only 基线)");
		goto restore;
	}
	baseline_counts = adc_base;
	cal_valid = true;

	int32_t base_pa = (int32_t)(((int64_t)adc_base * fsr_true) / 65536);

	LOG_INF("  步3 工作档 offset-only counts=%5u(= 零电流基线)→ 等效 %d pA",
		adc_base, base_pa);
	LOG_INF("  ✅ 标定生效:FSR=%d pA / 基线 counts=%u", fsr_cal_pa, baseline_counts);
	LOG_INF("     换算改用差分式 ⇒ ADC 固有偏移(±80LSB)与 offset 源容差(±22%%)双双抵消");

restore:
	/* 恢复:工作量程 + 工作 offset + 信号+offset 模式 */
	(void)set_fsr_and_offset(WP_FSR, WP_OFFSET_SEL);
	(void)max30131_spi_write_reg(MAX30131_REG_S1_CONFIG5,
					 max30131_enc_s1_config5(WP_CONV_TIME_CODE, true));
	(void)set_ioffset_conv(0U);
	if (!cal_valid) {
		LOG_WRN("⚠️ 标定未生效,退回标称值换算(绝对精度受 offset 源 ±22%% 容差污染)");
	}
	LOG_INF("──── 标定结束 ────");
}

static void selftest_diagnose_baseline(void)
{
	int32_t fsr_pa = max30131_fsr_pa(WP_FSR);
	int32_t off_pa = max30131_offset_pa(WP_OFFSET_SEL, WP_FSR);
	uint16_t counts;
	uint8_t tag;

	LOG_INF("──── 开机自检:基线定性(E 阶梯 + offset-only)────");
	log_status1("(自检前)");

	/* ---- 第一路证据:E 阶梯 ---- */
	static const int32_t e_ladder_mv[] = { 0, -100, -200 };

	LOG_INF("[E 阶梯] 漏电是欧姆的(I∝E);offset 源与 E 无关");
	for (size_t i = 0; i < ARRAY_SIZE(e_ladder_mv); i++) {
		if (set_polarization(e_ladder_mv[i]) != 0) {
			continue;
		}
		if (manual_convert_once(&counts, &tag) != 0) {
			continue;
		}
		int32_t fa = max30131_counts_to_reduction_fa(counts, fsr_pa, off_pa);

		LOG_INF("  E=%4d mV → counts=%5u  I=%8d fA (%d.%03d pA)  tag=0x%02x",
			e_ladder_mv[i], counts, fa, fa / 1000,
			(fa < 0 ? -fa : fa) % 1000, tag);
	}

	/* ---- 第二路证据(独立):offset-only 转换 ---- */
	/*
	 * IOFFSET_CONV=01 ⇒ 只转换内部 offset 电流,信号路径不参与。
	 * 读数直接就是 offset 源在本档下的 ADC 值:
	 *   若 ≈13107(=标称 10nA)⇒ offset 源正常 ⇒ 1.2nA 来自信号路径 = 漏电(B)
	 *   若 ≈11522(=8.79nA)  ⇒ offset 源本身偏低 ⇒ 1.2nA 是容差假象(A)
	 * 🔴 与 E 阶梯互为**独立**证据(不同机制),两者同向才算定性。
	 */
	LOG_INF("[offset-only] IOFFSET_CONV=01,直接量 offset 源真值");
	if (max30131_spi_write_reg(MAX30131_REG_CONVERT_SETUP1,
				   max30131_enc_convert_setup1(false, 0x1U, false,
							       WP_SENS_PERIOD_CODE)) == 0) {
		if (manual_convert_once(&counts, &tag) == 0) {
			int32_t ioff = max30131_cal_ioffset_pa(counts, fsr_pa);

			LOG_INF("  FIFO counts=%5u  → offset 源实测 %d pA(标称 %d pA,"
				"偏差 %d%%)", counts, ioff, off_pa,
				(int)(((int64_t)ioff - off_pa) * 100 / off_pa));
		}
		/* 另读 0x2B/0x2C S1_IOFFSET 寄存器作交叉核对 */
		uint8_t io[2] = { 0, 0 };

		if (max30131_spi_read_burst(MAX30131_REG_S1_IOFFSET_H, io, sizeof(io)) == 0) {
			uint16_t v = (uint16_t)(((uint16_t)io[0] << 8) | io[1]);

			LOG_INF("  S1_IOFFSET 寄存器 = %5u(与上面 FIFO 值应一致)", v);
		}
		/* 恢复正常测量配置 */
		(void)max30131_spi_write_reg(MAX30131_REG_CONVERT_SETUP1,
					     max30131_enc_convert_setup1(false, 0x0U, false,
									 WP_SENS_PERIOD_CODE));
	}

	/* 恢复工作点极化,并回到 AUTO */
	(void)set_polarization(WP_E_MV);
	log_status1("(自检后)");
	LOG_INF("──── 自检结束,恢复 E=%d mV 并进入 AUTO ────", WP_E_MV);
}

/* ------------------------------------------------------------------ */
/* 轮询取数                                                            */
/* ------------------------------------------------------------------ */
/* 读取 FIFO,最多上报 max_emit 个 S1-DC 样本,返回实际数量。 */
static uint16_t drain_fifo(uint16_t max_emit)
{
	uint8_t cnt_raw[2] = { 0, 0 };
	uint16_t emitted = 0U;

	/* 0x0C/0x0D 连续两字节:普通寄存器突发读会自增地址 */
	if (max30131_spi_read_burst(MAX30131_REG_FIFO_COUNTER1, cnt_raw,
				    sizeof(cnt_raw))) {
		return 0U;
	}

	/*
	 * 🔴 位布局是**打包**的,不是两个独立字节(regs.h Table 6):
	 *     0x0C FIFO_COUNTER1: bit[7] = DATA_COUNT[8] ｜ bit[6:0] = OVF_COUNTER
	 *     0x0D FIFO_COUNTER2: DATA_COUNT[7:0]
	 * 所以 DATA_COUNT 是 **9 位**(0..256),OVF_COUNTER 是 **7 位**,共用第一字节。
	 * 常见错法:`(c1 << 8) | c2` —— 那会把溢出计数当成数据计数的高位,
	 * FIFO 一溢出就读出天文数字并疯狂空读。
	 */
	uint16_t data_count = (uint16_t)((((uint16_t)cnt_raw[0] >> 7) << 8) | cnt_raw[1]);
	uint8_t ovf = (uint8_t)(cnt_raw[0] & 0x7FU);

	uint16_t avail = max30131_fifo_available(ovf, data_count);

	if (avail == 0U) {
		return 0U;
	}
	if (ovf != 0U) {
		/* 溢出说明轮询跟不上 —— 上位机 check_integrity() 也会独立发现 */
		LOG_WRN("⚠️ FIFO 溢出 %u 次,轮询间隔 %d ms 偏长", ovf, POLL_INTERVAL_MS);
	}

	/*
	 * 🔴 标定成功就用 **FSR 真值 + 差分换算**(ADC 固有偏移与 offset 源容差抵消);
	 * 否则退回标称值 + 绝对 offset 换算,并在日志里说清精度受污染。
	 */
	int32_t fsr_pa = cal_valid ? fsr_cal_pa : max30131_fsr_pa(WP_FSR);
	int32_t off_pa = max30131_offset_pa(WP_OFFSET_SEL, WP_FSR);

	for (uint16_t i = 0; i < avail; i++) {
		uint8_t raw[3];

		if (max30131_spi_read_burst(MAX30131_REG_FIFO_DATA, raw, sizeof(raw))) {
			return emitted;
		}

		max30131_fifo_word_t s;

		if (max30131_fifo_unpack(raw, &s) != MAX30131_OK) {
			LOG_WRN("FIFO 解包失败,丢弃一样本");
			continue;
		}

		/*
		 * 🔴 读空 FIFO 会返回哨兵 tag 0xFE —— 必须识别并停,
		 * 否则 data_count 与实际不一致时会把哨兵当数据上报。
		 */
		if (s.tag_is_8bit && s.tag == MAX30131_FIFO_TAG_EMPTY) {
			LOG_WRN("读到 FIFO 空哨兵(tag=0xFE),本轮提前收手"
				"(data_count=%u 与实际不符)", data_count);
			return emitted;
		}

		/*
		 * 电极电压词(tag 0xD0–0xD3)走电压路径。它们由 System ADC 在
		 * AUTO 模式下按 SYS_PERIOD 自主转换,与 Sensor ADC 的电流转换**并行**
		 * (p143:"all selected channels are continuously converted at the rate
		 * determined by each channels period setting in SENS_PERIOD[3:0],
		 * TEMP_PERIOD[3:0], and SYS_PERIOD[3:0]"),共用同一个 FIFO、靠 tag 区分。
		 */
		if (collect_voltage_word(&s)) {
			continue;
		}

		/* 只上报「Sensor 1 DC 电流」样本;其它 tag(如 EIS)本版不处理 */
		if (s.tag != MAX30131_FIFO_TAG_S1_DC) {
			LOG_DBG("跳过非 S1-DC 样本 tag=0x%02x", s.tag);
			continue;
		}

		/* 🔴 换算走 lib 的 fA 口径(见 max30131.h:为何不能用 pA) */
		int32_t fa = cal_valid
			? max30131_reduction_from_counts_diff_fa(baseline_counts,
								 s.counts, fsr_pa)
			: max30131_counts_to_reduction_fa(s.counts, fsr_pa, off_pa);

		/*
		 * 🔴 饱和时读数**不再是测量**(恒电位环已开环),必须标记,
		 * 否则上位机会把一段废数据当真数据去算 σ 与标定曲线。
		 */
		uint8_t sat = max30131_saturation_flags(s.counts, sat_margin_counts());

		if (sat != 0U && sat != last_sat) { /* 只在状态翻转时告警,避免刷屏 */
			if (sat & MAX30131_SAT_LOW) {
				LOG_ERR("🔴 饱和(LOW):counts=%u 逼近 0 —— 还原电流吃光 offset"
					"(上限 %d pA),WE 已失恒电位控制,本段数据作废",
					s.counts, max30131_max_reduction_pa(off_pa));
			}
			if (sat & MAX30131_SAT_HIGH) {
				LOG_ERR("🔴 饱和(HIGH):counts=%u 逼近满量程 —— 氧化方向超出"
					" %d pA", s.counts,
					max30131_max_oxidation_pa(fsr_pa, off_pa));
			}
		} else if (sat == 0U && last_sat != 0U) {
			LOG_INF("✅ 退出饱和,counts=%u", s.counts);
		}
		last_sat = sat;

		emit_sample(s.counts, fa, s.tag, s.auto_mode, ovf, sat);
		emitted++;
		if (emitted >= max_emit) {
			break;
		}
	}

	return emitted;
}

/* ------------------------------------------------------------------ */
/*
 * 开机装载配置。初值全部取自 measurement_config.h ⇒ 不发任何命令时行为与
 * 改动前逐位一致(唯一例外是 conv:它现在由 auto 派生,现行配置得到 0x2 而非 0x1,
 * 这是刻意的改进,理由见 WP_CONV_TIME_CODE 处)。
 *
 * 🔴 `CFG_BOOT` + `CFG_DERIVED` 是本次改动里性价比最高的两行:纯新增打印、
 *    零行为变化,却让"idle 窗口 51.5%"这类数字从**我算出来的**变成**设备打出来的**。
 *    此前一份旧 CSV 事后完全无法确定它是哪个档、哪个积分时间采的。
 */
static void cfg_load_defaults(void)
{
	memset(&cfg_live, 0, sizeof(cfg_live));
	cfg_live.fsr = GUI_WP_FSR;
	cfg_live.off = GUI_WP_OFFSET_SEL;
	cfg_live.conv_pinned = false;          /* ⇒ 由 auto 派生 */
	cfg_live.period = GUI_SENS_PERIOD_CODE;
	cfg_live.sysper = WP_DEFAULT_SYS_PERIOD_CODE;
	cfg_live.clk40 = WP_DEFAULT_CLK_40K;
	cfg_live.ioc = 0U;
	cfg_live.chop = true;
	cfg_live.rs = false;
	cfg_live.ios = WP_DEFAULT_IOS;
	cfg_live.e_mv = GUI_WP_E_MV;
	cfg_live.vwe_mv = WP_DEFAULT_V_WE_MV;
	cfg_live.idle = WP_DEFAULT_IDLE_MODE;
	cfg_live.cellv = WP_DEFAULT_CELLV_ENABLE;
	cfg_live.satpct = SAT_MARGIN_DEFAULT_PCT;
	cfg_live.sensor_selected = true;
	cfg_live.amps_on = true;

	afe_cfg_derive(&cfg_live, &drv_live);
	cfg_epoch = 1U;
	printk("CFG_BOOT ep=%u ms=%lld fw=%s reason=boot\n", cfg_epoch,
	       (long long)k_uptime_get(), "v4-dbg1");
}

int main(void)
{
	LOG_INF("=== pA-Converter V4.0 固件启动(轮询模式 / 无 BLE)===");
	cfg_load_defaults();

	/* 三条焊死项:DCDCEN=0 断言 + POFCON 2.0V + 看门狗 */
	if (board_guards_init() != 0) {
		LOG_ERR("板级守卫未就绪,停在此处(不进入采集)");
		while (1) {
			k_msleep(1000);
		}
	}

	if (max30131_spi_init() != 0 || afe_probe() != 0) {
		LOG_ERR("AFE 探测失败,停在此处");
		while (1) {
			board_guards_feed();
			k_msleep(1000);
		}
	}

	if (afe_configure() != 0) {
		LOG_ERR("AFE 配置失败,停在此处");
		while (1) {
			board_guards_feed();
			k_msleep(1000);
		}
	}
	/*
	 * 🔴 2026-08-10 实测发现的缺口:电位连采号称"常开",但 System ADC 的自主转换
	 * 靠的是 AUTO=1,而 afe_configure() **刻意不写 0x83**(为了让开机自检能用手动
	 * 转换),AUTO 直到首轮 afe_start_auto() 才起来。
	 * ⇒ 开机到第一轮之间(以及 Quiet Time 期间)一个 CELL_V 都不会有。
	 *   首烧日志里 11.6s 的 wait_for_start_command 期间零电压词,就是这个原因,
	 *   **不是** 0x55 的 SYS_SELECT 位号猜错。
	 * 自检/标定两条路径都已关(WP_RUN_* 皆 false),所以这里直接起 AUTO 无冲突;
	 * 将来若重开自检,必须把 AUTO 挪到自检之后。
	 */
	if (WP_CELLV_ENABLE && !WP_RUN_STARTUP_DIAGNOSTIC &&
	    !WP_RUN_AFE_GAIN_CALIBRATION) {
		if (afe_start_auto() == 0) {
			LOG_INF("电位连采:开机即起 AUTO(System ADC 与电流转换并行,"
				"idle 期间也在采)");
		}
	}
	/* 配置已落到器件上 ⇒ 立即把「设备自己认为的状态」整套打出来 */
	replay_state("boot");

	if (WP_RUN_STARTUP_DIAGNOSTIC) {
		board_guards_feed();
		selftest_diagnose_baseline();
		board_guards_feed();
	}

	if (WP_RUN_AFE_GAIN_CALIBRATION) {
		/* 传统双档增益标定;10Hz 工作流改由上位机用最后20s数据标定。 */
		afe_calibrate();
	} else {
		LOG_INF("10Hz 工作流:跳过 AFE 双档标定,由上位机拟合最后20s浓度标定曲线");
	}
	board_guards_feed();

	uint32_t run_number = 0U;
	bool start_pending = false;
	while (1) {
		if (!start_pending) {
			wait_for_start_command();
		}
		start_pending = false;
		run_number++;
		last_sat = 0U;
		printk("IT_START run=%u target_mv=%d\n", run_number, WP_E_MV);

	/*
	 * 🔴 次序:先退出 idle(开放大器)→ 再加 Quiet Time 电位 → 才能静置。
	 * 原来的 prestep 只是 sleep,它**默认电位已经加着**;在 IDLE_DISCONNECT 下
	 * 放大器是关的,不先做这两步就是干等,双电层根本没在充。
	 */
	exit_idle_state();
	if (set_polarization(WP_STARTUP_E_MV) != 0) {
		LOG_ERR("施加静置电位 E=%d mV 失败,停在此处", WP_STARTUP_E_MV);
		while (1) {
			board_guards_feed();
			k_msleep(1000);
		}
	}
	if (WP_IDLE_MODE == IDLE_DISCONNECT && WP_PRESTEP_DURATION_MS == 0U) {
		/*
		 * 断开模式 + 零 Quiet Time = 每轮都在录双电层充电瞬态,不是稳态。
		 * 实测 τ≈26–50s ⇒ 需要 130–250s。CHI 手册示例是 2s,但那是宏电极;
		 * 我们的界面是 CPE,慢两个数量级,照抄 2s 会全废。
		 */
		LOG_ERR("🔴 IDLE_DISCONNECT 但 Quiet Time = 0!每轮开头都是双电层充电瞬态。"
			"请在上位机把 prestep_s 设为 180~300s(实测 τ≈26–50s ⇒ 5τ≈130~250s)");
	}

	if (WP_PRESTEP_DURATION_MS > 0U) {
		LOG_INF("Quiet Time(静置):E=%d mV 保持 %u ms 后才开始记录"
			"(对应 CHI 的 Quiet Time = quiescent time before potential scan)",
			WP_STARTUP_E_MV, WP_PRESTEP_DURATION_MS);
		uint32_t waited_ms = 0U;
		while (waited_ms < WP_PRESTEP_DURATION_MS) {
			/*
			 * 🔴 静置期不能只 sleep:此时 AUTO 在跑、System ADC 每 SYS_PERIOD
			 * 产一组电压词,没人读就会在 FIFO 里滚掉(ro=1)。而静置期恰恰是
			 * 最该看电位的一段(双电层在充,E 应当从 OCP 收敛到设定值)。
			 * chunk 从 1000ms 缩到 POLL_INTERVAL_MS,顺带让 STATUS1 心跳也活着。
			 */
			uint32_t chunk_ms = MIN((uint32_t)POLL_INTERVAL_MS,
						WP_PRESTEP_DURATION_MS - waited_ms);
			board_guards_feed();
			if (WP_CELLV_ENABLE) {
				drain_fifo_for_voltages();
			}
			afe_status_poll("quiet", false);
			(void)poll_control_command();   /* 静置期也要能收 STOP / SET */
			k_msleep(chunk_ms);
			waited_ms += chunk_ms;
		}
	}

	const char *step_direction = WP_E_MV < WP_STARTUP_E_MV ? "高→低" :
		WP_E_MV > WP_STARTUP_E_MV ? "低→高" : "无阶跃";
	LOG_INF("IT 电位阶跃:%d → %d mV(%s)", WP_STARTUP_E_MV, WP_E_MV,
		step_direction);
	/* 正式 i-t 测量从用户配置的起始电位阶跃到目标电位。 */
	if (set_polarization(WP_E_MV) != 0) {
		LOG_ERR("施加测量电位 E=%d mV 失败,停在此处", WP_E_MV);
		while (1) {
			board_guards_feed();
			k_msleep(1000);
		}
	}
	LOG_INF("i-t 测量电位已施加:E=%d mV, duration=%d ms", WP_E_MV,
		WP_MEASUREMENT_DURATION_MS);

	/*
	 * MAX30131 的 SENS_PERIOD 最短约 124ms,硬件原生上限约 8.06Hz,达不到
	 * 用户要求的 10Hz。这里使用芯片 AUTO 连续转换,先采集 1452 个原生样本
	 * (约 180s),上位机再按时间戳重采样为 10Hz/1800 点。这样不会把手动
	 * CONVERT 位误当成单次触发,也能避免 FIFO 在高频轮询时被反复清空。
	 */
	if (max30131_spi_write_reg(MAX30131_REG_FIFO_CONFIG2,
					max30131_enc_fifo_config2(true, true, false, true)) != 0 ||
		afe_start_auto() != 0) {
		LOG_ERR("AUTO i-t 转换启动失败");
		(void)set_polarization(WP_STARTUP_E_MV);
		while (1) {
			board_guards_feed();
			k_msleep(1000);
		}
	}

	int64_t measurement_start_ms = k_uptime_get();
	int64_t next_potential_audit_ms = measurement_start_ms + 1000;
	uint32_t native_samples = 0U;
	uint32_t conversion_errors = 0U;
	bool potential_fault = false;
	bool restart_requested = false;
	bool stop_requested = false;
	bool afe_fault = false;

	acquiring = true;
	run_tainted = false;
	afe_fault_invalid = false;
	afe_fault_vdd = false;
	invalid_cfg_streak = 0U;
	LOG_INF("进入 AUTO i-t 采集: %u native samples (约8Hz; host重采样10Hz), E=%d mV",
		expected_sample_count(), WP_E_MV);

	while (native_samples < expected_sample_count()) {
		board_guards_feed();
		enum control_command command = poll_control_command();
		if (command == CONTROL_START) {
			restart_requested = true;
			break;
		}
		if (command == CONTROL_STOP) {
			stop_requested = true;
			break;
		}
		uint16_t left = (uint16_t)(expected_sample_count() - native_samples);
		uint16_t n = drain_fifo(left);
		native_samples += n;
		int64_t now_ms = k_uptime_get();

		if (now_ms >= next_potential_audit_ms) {
			if (audit_polarization(WP_E_MV, native_samples) != 0) {
				potential_fault = true;
				break;
			}
			/*
			 * 🔴 STATUS1 搭这趟便车(不另起周期,见 afe_status_poll 注释)。
			 * 边沿触发 + 30s 心跳,采集中连续两次 INVALID_CFG 或一次
			 * VDD_OOR 就中止本轮 —— 那之后的读数物理上不再是测量。
			 */
			afe_status_poll("run", false);
			if (afe_fault_invalid || afe_fault_vdd) {
				afe_fault = true;
				break;
			}
			do {
				next_potential_audit_ms += 1000;
			} while (next_potential_audit_ms <= now_ms);
		}

		if (n == 0U) {
			conversion_errors++;
		}
		if (native_samples >= expected_sample_count()) {
			break;
		}
		if (k_uptime_get() - measurement_start_ms >
			WP_MEASUREMENT_DURATION_MS + 10000) {
			LOG_ERR("AUTO i-t 超过时限:仅获得 %u/%u 个原生样本",
				native_samples, expected_sample_count());
			break;
		}
		k_msleep(POLL_INTERVAL_MS);
	}

	/*
	 * 先把电位写回起始值,再按 WP_IDLE_MODE 处置电解池 —— 次序不能反:
	 * DISCONNECT 会关掉放大器,关掉之后再写 DAC 就是空动作。
	 *
	 * ⚠️ 注:在 DISCONNECT 模式下这次 set_polarization 本身**不保持任何电位**
	 * (放大器随后就关了),它只是让寄存器状态回到已知值。对应 CH Instruments 的
	 * "Return to Initial E after Run" 选项 —— 手册明说该选项
	 * "only makes sense to enable ... if Cell On Between Runs is also checked"。
	 */
	acquiring = false;
	(void)set_polarization(WP_STARTUP_E_MV);
	enter_idle_state();
	LOG_INF("i-t 测量结束:elapsed=%lld ms,native=%u/%u,empty polls=%u,E 已恢复为 %d mV",
		(long long)(k_uptime_get() - measurement_start_ms), native_samples,
		expected_sample_count(), conversion_errors, WP_STARTUP_E_MV);
	if (restart_requested || stop_requested || afe_fault) {
		const char *reason = afe_fault
					     ? (afe_fault_invalid ? "invalid_cfg"
								  : "vdd_oor")
					     : (restart_requested ? "restart"
								  : "stop");

		printk("IT_ABORTED reason=%s native=%u elapsed_ms=%lld ep=%u "
		       "tainted=%d\n", reason, native_samples,
		       (long long)(k_uptime_get() - measurement_start_ms),
		       cfg_epoch, run_tainted ? 1 : 0);
		if (afe_fault) {
			LOG_ERR("🔴 本轮因 AFE 状态位异常中止(%s)—— 该段读数物理上"
				"不再是测量,禁止用于标定/预测", reason);
		}
		start_pending = restart_requested;
		continue;
	}
	if (potential_fault) {
		LOG_ERR("本轮因电位寄存器审计失败而提前结束;原始数据保留但不得用于标定/预测");
	}
	/* 机器可读完成标记:上位机收到后立即收尾,不再等待 duration/idle timeout。 */
	printk("IT_DONE native=%u expected=%u elapsed_ms=%lld ep=%u tainted=%d\n",
	       native_samples, expected_sample_count(),
	       (long long)(k_uptime_get() - measurement_start_ms), cfg_epoch,
	       run_tainted ? 1 : 0);
	}

	return 0;
}
