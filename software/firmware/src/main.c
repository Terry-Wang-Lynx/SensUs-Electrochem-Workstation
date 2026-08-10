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
 */
static max30131_fsr_t        wp_fsr        = GUI_WP_FSR;
#define WP_FSR        wp_fsr
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
static max30131_offset_sel_t wp_offset_sel = GUI_WP_OFFSET_SEL;
#define WP_OFFSET_SEL wp_offset_sel

/* 方案 C:在线切档。定义在 set_fsr_and_offset() 之后,这里先前向声明。 */
static int apply_range(int fsr_code, int off_code);
#define WP_REF        MAX30131_REF_1536MV      /* 内部 1.536V;CR2032 EOL 2.0V 只此档 */

#define WP_V_WE_MV    400                      /* WE 电位 0.4V */
#define WP_E_MV       GUI_WP_E_MV              /* E = V_WE - V_RE */
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

/* ADC 时钟源:false = 34.952kHz(慢钟),true = 40.96kHz。 */
#define WP_CLK_40K    false

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
#define WP_S1_CONFIG3 0x08U   /* IOS_MODE=1=常在(寄存器图 p96,唯一自洽);A/B 组 B 的 0x00 已阴性 */

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
 */
#define IDLE_STOP_CONV   0
#define IDLE_KEEP_BIASED 1
#define IDLE_DISCONNECT  2
#define WP_IDLE_MODE IDLE_DISCONNECT

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
#define WP_CELLV_ENABLE true
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
#define WP_SYS_PERIOD_CODE 0x3U

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
#define WP_CONV_TIME_CODE \
	(max30131_fsr_uses_fast_clock(WP_FSR) ? 0x1U : 0x0U)
#define WP_SENS_PERIOD_CODE GUI_SENS_PERIOD_CODE

/* 每批攒多少样本再取。轮询模式下这只决定读取粒度,不再是唤醒条件。 */
#define WP_BATCH_SAMPLES 16U

/* 本轮 i-t 试验的电位保持时间。到时后固件停转换并保持配置的空闲电位。 */
#define WP_MEASUREMENT_DURATION_MS GUI_MEASUREMENT_DURATION_MS
#define WP_EXPECTED_SAMPLE_COUNT  ((WP_MEASUREMENT_DURATION_MS + GUI_SENS_PERIOD_MS - 1U) / GUI_SENS_PERIOD_MS)

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
 */
#define SAT_MARGIN_FRACTION_PCT 5      /* 取零电流码的 5% */
#define SAT_MARGIN_COUNTS_MAX   1311U  /* 2% FS,原值,作上限(不回归) */
#define SAT_MARGIN_COUNTS_MIN   8U     /* ≥1 个 13bit 码步长 */

static uint16_t sat_margin_counts(void)
{
	int32_t fsr_pa = max30131_fsr_pa(WP_FSR);
	int32_t off_pa = max30131_offset_pa(WP_OFFSET_SEL, WP_FSR);
	int64_t zero_code, m;

	if (fsr_pa <= 0 || off_pa <= 0) {
		return SAT_MARGIN_COUNTS_MAX;
	}
	zero_code = ((int64_t)off_pa * 65536) / fsr_pa;   /* I=0 对应的 counts */
	m = zero_code * SAT_MARGIN_FRACTION_PCT / 100;
	if (m > (int64_t)SAT_MARGIN_COUNTS_MAX) {
		m = SAT_MARGIN_COUNTS_MAX;
	}
	if (m < (int64_t)SAT_MARGIN_COUNTS_MIN) {
		m = SAT_MARGIN_COUNTS_MIN;
	}
	return (uint16_t)m;
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
 * 🔴 电流单位是 **整数 fA** 不是 pA —— 50nA 档 LSB 约为 763 fA,
 *    用 pA 会让协议本身比器件还粗、把亚 pA 噪声量化掉。
 * 用 printk 而不是 LOG_*:LOG 会加时间戳/等级前缀,破坏行协议。
 * 诊断信息仍走 LOG_*,record.py 会忽略不匹配的行。
 */
static void emit_sample(uint16_t counts, int32_t fa, uint8_t tag, bool auto_mode,
			uint8_t ovf, uint8_t sat)
{
	printk("S seq=%u ms=%u counts=%u fa=%d tag=%u auto=%u ovf=%u sat=%u\n",
	       seq++, (uint32_t)k_uptime_get_32(), counts, fa, tag,
	       auto_mode ? 1U : 0U, ovf, sat);
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
		{ 0x80U, 0x00U, "CONVERT SETUP1: DC, IOFFSET_CONV=00, SENS_PERIOD=0000(124ms)" },
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

		if (max30131_spi_read_reg(MAX30131_REG_STATUS1, &st)) {
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

	if (max30131_spi_read_reg(MAX30131_REG_STATUS1, &st)) {
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

static enum control_command poll_control_command(void)
{
	static char command[32];   /* 16→32:要装下 "RANGE <fsr> <sel>" */
	static size_t used;
	char ch;

	while (SEGGER_RTT_Read(0U, &ch, 1U) == 1U) {
		if (ch == '\r' || ch == '\n') {
			command[used] = '\0';
			used = 0U;
			if (strcmp(command, "START") == 0) {
				return CONTROL_START;
			}
			if (strcmp(command, "STOP") == 0) {
				return CONTROL_STOP;
			}
			/*
			 * `RANGE <fsr_code> <offset_sel>` —— 在线切档,**就地生效**。
			 * 刻意不返回新的 control_command:两个调用点
			 * (wait_for_start_command / 采集循环)都能白拿到这个能力,
			 * 不必改它们的 START/STOP 语义 —— 那是本轮已经踩过两次回归的地方。
			 * 切档不打断本轮采集,也不动极化。
			 */
			if (strncmp(command, "RANGE ", 6) == 0) {
				int a = -1, b = -1;

				if (sscanf(command + 6, "%d %d", &a, &b) == 2) {
					(void)apply_range(a, b);
				} else {
					printk("RANGE_REJECT reason=parse raw=%s\n",
					       command);
				}
				continue;
			}
			continue;
		}
		if (used + 1U < sizeof(command)) {
			command[used++] = ch;
		} else {
			used = 0U;
		}
	}
	return CONTROL_NONE;
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
		return max30131_spi_write_reg(MAX30131_REG_S1_CONFIG1,
					      s1_config1_byte(true, true));
	}
	rc = max30131_spi_write_reg(MAX30131_REG_S1_CONFIG1,
				   s1_config1_byte(false, true));
	if (rc) {
		return rc;
	}
	return max30131_spi_write_reg(MAX30131_REG_S1_CONFIG1,
				      s1_config1_byte(false, false));
}

/* 选/不选 Sensor 1 参与转换(0x24 bit0)。idle 探测期间必须**不选**——见下。 */
static int set_sensor_selected(bool selected)
{
	return max30131_spi_write_reg(MAX30131_REG_S1_CONFIG5,
				      max30131_enc_s1_config5(WP_CONV_TIME_CODE,
							      selected));
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
	int64_t warned_at;
} cellv;

#define CELLV_WE 0
#define CELLV_RE 1
#define CELLV_CE 2
#define CELLV_WO 3

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

	cellv.code[idx] = w->counts;
	cellv.mv[idx] = max30131_sys_adc_mv(w->counts, max30131_ref_mv(WP_REF),
					    WP_SYSADC_SENSV_GAIN);
	cellv.got[idx] = true;
	cellv.ever_got = true;

	/* WE 与 RE 都到手才算一组 —— 少任何一个都算不出 E。 */
	if (!cellv.got[CELLV_WE] || !cellv.got[CELLV_RE]) {
		return true;
	}
	printk("CELL_V ms=%lld idle=%d we_mv=%d re_mv=%d ce_mv=%d wo_mv=%d "
	       "e_mv=%d we_code=%u re_code=%u ce_code=%u wo_code=%u\n",
	       (long long)k_uptime_get(), WP_IDLE_MODE,
	       cellv.mv[CELLV_WE], cellv.mv[CELLV_RE],
	       cellv.got[CELLV_CE] ? cellv.mv[CELLV_CE] : -1,
	       cellv.got[CELLV_WO] ? cellv.mv[CELLV_WO] : -1,
	       cellv.mv[CELLV_WE] - cellv.mv[CELLV_RE],
	       cellv.code[CELLV_WE], cellv.code[CELLV_RE],
	       cellv.code[CELLV_CE], cellv.code[CELLV_WO]);
	for (int i = 0; i < 4; i++) {
		cellv.got[i] = false;
	}
	return true;
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

		if (max30131_spi_read_reg(MAX30131_REG_STATUS1, &st) != 0) {
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
		(void)set_sensor_selected(true);
		if (!WP_CELLV_ENABLE) {
			/* 纯原始路径下 idle 停过转换,这里要重新起 AUTO。 */
			(void)max30131_spi_write_reg(MAX30131_REG_CONVERT_START,
						     max30131_enc_convert_start(true, true));
		}
	}
}

static void wait_for_start_command(void)
{
	printk("IT_READY target_mv=%d idle_mode=%d cellv=%d\n", WP_E_MV,
	       WP_IDLE_MODE, (int)WP_CELLV_ENABLE);
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
 */
static int apply_range(int fsr_code, int off_code)
{
	max30131_fsr_t f;
	max30131_offset_sel_t o;
	uint8_t ct;
	max30131_err_t e;
	uint8_t bits;

	if (fsr_code < 0 || fsr_code > 5 || off_code < 0 || off_code > 7) {
		printk("RANGE_REJECT reason=arg fsr=%d offset_sel=%d\n",
		       fsr_code, off_code);
		return -EINVAL;
	}
	f = (max30131_fsr_t)fsr_code;
	o = (max30131_offset_sel_t)off_code;
	ct = max30131_fsr_uses_fast_clock(f) ? 0x1U : 0x0U;

	e = max30131_check_period_vs_conv(ct, WP_SENS_PERIOD_CODE, WP_CLK_40K, f);
	if (e != MAX30131_OK) {
		printk("RANGE_REJECT reason=period fsr=%d conv_code=%u err=%d\n",
		       fsr_code, ct, (int)e);
		return -EINVAL;
	}
	/* offset 不能超过 FSR,否则还原侧上限无意义、氧化侧为 0 */
	if (max30131_offset_pa(o, f) > max30131_fsr_pa(f)) {
		printk("RANGE_REJECT reason=offset_gt_fsr fsr=%d offset_sel=%d\n",
		       fsr_code, off_code);
		return -EINVAL;
	}
	if (set_fsr_and_offset(f, o) != 0 ||
	    max30131_spi_write_reg(MAX30131_REG_S1_CONFIG5,
				   max30131_enc_s1_config5(ct, true)) != 0) {
		printk("RANGE_REJECT reason=spi fsr=%d offset_sel=%d\n",
		       fsr_code, off_code);
		return -EIO;
	}
	wp_fsr = f;
	wp_offset_sel = o;
	bits = max30131_conv_time_bits(ct);

	printk("RANGE_APPLIED fsr_code=%d offset_sel=%d fsr_pa=%d off_pa=%d "
	       "bits=%u lsb_eff_fa=%d sat_margin=%u red_max_pa=%d ox_max_pa=%d\n",
	       fsr_code, off_code, max30131_fsr_pa(f), max30131_offset_pa(o, f),
	       bits, max30131_lsb_fa(f) << (16U - bits), sat_margin_counts(),
	       max30131_max_reduction_pa(max30131_offset_pa(o, f)),
	       max30131_max_oxidation_pa(max30131_fsr_pa(f),
					 max30131_offset_pa(o, f)));
	return 0;
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
int main(void)
{
	LOG_INF("=== pA-Converter V4.0 固件启动(轮询模式 / 无 BLE)===");

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
			uint32_t chunk_ms = MIN(1000U, WP_PRESTEP_DURATION_MS - waited_ms);
			board_guards_feed();
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
	LOG_INF("进入 AUTO i-t 采集: %u native samples (约8Hz; host重采样10Hz), E=%d mV",
		WP_EXPECTED_SAMPLE_COUNT, WP_E_MV);

	while (native_samples < WP_EXPECTED_SAMPLE_COUNT) {
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
		uint16_t left = (uint16_t)(WP_EXPECTED_SAMPLE_COUNT - native_samples);
		uint16_t n = drain_fifo(left);
		native_samples += n;
		int64_t now_ms = k_uptime_get();

		if (now_ms >= next_potential_audit_ms) {
			if (audit_polarization(WP_E_MV, native_samples) != 0) {
				potential_fault = true;
				break;
			}
			do {
				next_potential_audit_ms += 1000;
			} while (next_potential_audit_ms <= now_ms);
		}

		if (n == 0U) {
			conversion_errors++;
		}
		if (native_samples >= WP_EXPECTED_SAMPLE_COUNT) {
			break;
		}
		if (k_uptime_get() - measurement_start_ms >
			WP_MEASUREMENT_DURATION_MS + 10000) {
			LOG_ERR("AUTO i-t 超过时限:仅获得 %u/%u 个原生样本",
				native_samples, WP_EXPECTED_SAMPLE_COUNT);
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
	(void)set_polarization(WP_STARTUP_E_MV);
	enter_idle_state();
	LOG_INF("i-t 测量结束:elapsed=%lld ms,native=%u/%u,empty polls=%u,E 已恢复为 %d mV",
		(long long)(k_uptime_get() - measurement_start_ms), native_samples,
		WP_EXPECTED_SAMPLE_COUNT, conversion_errors, WP_STARTUP_E_MV);
	if (restart_requested || stop_requested) {
		const char *reason = restart_requested ? "restart" : "stop";
		printk("IT_ABORTED reason=%s native=%u elapsed_ms=%lld\n", reason,
		       native_samples,
		       (long long)(k_uptime_get() - measurement_start_ms));
		start_pending = restart_requested;
		continue;
	}
	if (potential_fault) {
		LOG_ERR("本轮因电位寄存器审计失败而提前结束;原始数据保留但不得用于标定/预测");
	}
	/* 机器可读完成标记:上位机收到后立即收尾,不再等待 duration/idle timeout。 */
	printk("IT_DONE native=%u expected=%u elapsed_ms=%lld\n", native_samples,
	       WP_EXPECTED_SAMPLE_COUNT,
	       (long long)(k_uptime_get() - measurement_start_ms));
	}

	return 0;
}
