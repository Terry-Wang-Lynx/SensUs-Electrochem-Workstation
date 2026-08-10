/*
 * max30131.h — MAX30131 纯逻辑层 API(零硬件依赖)
 *
 * 用途    : 把「会算错的东西」全部隔离到可在开发机上单测的纯函数里:
 *           寄存器位域编码、FIFO 词解包、counts↔电流换算、DAC 码↔电位、
 *           极化设定、共模余量校核、手动增益校准反算、时序表查询。
 *           传输层(SPI/GPIO)与 Zephyr 全部不在本文件内。
 * 用法    : 固件 app 层 include 本头,配合 max30131_transport.h 注入实际 SPI;
 *           主机侧单测直接编译 max30131.c(见 tests/Makefile)。
 * 前置条件: C99;只依赖 <stdint.h> <stdbool.h> <stddef.h>。无动态分配、无浮点。
 * 快照日期: 2026-07-27
 *
 * 单位约定(全库统一,避免量纲混乱):
 *   电流 = pA(int32_t;50nA 档满量程 50000pA,不会溢出)
 *   电压 = mV(int32_t)
 *   时间 = ms(int32_t)
 * 设计依据: docs/ver4.0/05-IC应用设计/U1-MAX30131-AFE应用.md(经 critic 定版)
 *           datasheet 章节见 max30131_regs.h 头部
 */

#ifndef MAX30131_H
#define MAX30131_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "max30131_regs.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ================================================================== */
/* 错误码                                                              */
/* ================================================================== */
typedef enum {
	MAX30131_OK = 0,
	MAX30131_ERR_ARG = -1,       /* 参数非法 */
	MAX30131_ERR_RANGE = -2,     /* 超出器件可表达范围 */
	MAX30131_ERR_HEADROOM = -3,  /* 共模/摆幅余量不足 */
	MAX30131_ERR_FIFO_EMPTY = -4,/* FIFO 空标记(tag 0xFE) */
	MAX30131_ERR_FIFO_TAG = -5,  /* 非预期 tag */
	MAX30131_ERR_CFG = -6,       /* 配置自相矛盾(如 period < conv time) */
} max30131_err_t;

/* ================================================================== */
/* 满量程档(Sn_FSR[2:0],0x23 bit[7:5])                              */
/* ================================================================== */
typedef enum {
	MAX30131_FSR_50NA = 0,
	MAX30131_FSR_100NA = 1,
	MAX30131_FSR_250NA = 2, /* 🔴 勘误:是 250nA,不是 200nA */
	MAX30131_FSR_500NA = 3,
	MAX30131_FSR_1000NA = 4,
	MAX30131_FSR_2000NA = 5,
	/* 110/111 = Reserved. Not used */
} max30131_fsr_t;

/* 满量程电流(pA);非法档返回 0 */
int32_t max30131_fsr_pa(max30131_fsr_t fsr);

/*
 * ADC 时钟分组:FSR 码 ≤3(50/100/250/500nA)走慢钟 34.952k/40.96kHz;
 * FSR 码 >3(1000/2000nA)全通道走 4× 钟。影响转换时间表选哪张。
 */
bool max30131_fsr_uses_fast_clock(max30131_fsr_t fsr);

/* 1 LSB 对应多少 fA(=FSR_pA*1000/65536),供口径纪律用:
 * 50nA 档 → 763 fA。⚠️ LSB 是量化步长,不是检测能力。 */
int32_t max30131_lsb_fa(max30131_fsr_t fsr);

/* ================================================================== */
/* offset 电流档(Sn_OFFSET_SEL[2:0],0x23 bit[2:0])                   */
/* ================================================================== */
/*
 * 🔴🔴 档位名用 datasheet 的 **typ** 值,不是"整数标称值"。
 * 2026-08-01 逐行核 datasheet Electrical Characteristics「Offset Current」表后更正:
 *
 *   SEL  min   typ   max   单位     曾经错写成
 *   ---  ----  ----  ----  ------   -----------
 *    1     9    10    11    %FS
 *    2    18    20    22    %FS
 *    3    45    50    55    %FS
 *    4     7   ★9★   11    nA      ❌ 曾按 10nA(超出 typ 11%)
 *    5    16   ★19★  22    nA      ❌ 曾按 20nA
 *    6    34    40    46    nA      ✅
 *    7    67    80    93    nA      ✅
 *
 * 🔴 注意容差:SEL=4 是 **7–11 nA,±22%**。所以 typ 值
 * **只能用于"offset 够不够盖住信号峰"的粗判,绝不能用于电流换算**——
 * datasheet 明令:换算前必须用 IOFFSET_CONV=1 实测该档 offset 真值
 * (见 max30131_cal_ioffset_pa / max30131_cal_fsr_pa 与
 *  docs/ver4.0/05-IC应用设计/ 的校准专章)。
 * 实测教训:按 10nA 标称换算,开路读数会凭空出现 ~1.2nA 的"电流",
 * 一度被怀疑成 165MΩ 漏电(2026-07-31,见 outputs/20260731-V4首烧与首份指标/)。
 */
typedef enum {
	MAX30131_OFFSET_0PCT = 0, /* 单极性;datasheet 明确不推荐(WE 放大器需有电流) */
	MAX30131_OFFSET_10PCT_FSR = 1,
	MAX30131_OFFSET_20PCT_FSR = 2,
	MAX30131_OFFSET_50PCT_FSR = 3,
	MAX30131_OFFSET_SEL4_9NA = 4,  /* typ 9nA(7–11) */
	MAX30131_OFFSET_SEL5_19NA = 5, /* typ 19nA(16–22) */
	MAX30131_OFFSET_SEL6_40NA = 6, /* typ 40nA(34–46) */
	MAX30131_OFFSET_SEL7_80NA = 7, /* typ 80nA(67–93) */
} max30131_offset_sel_t;

/*
 * datasheet **typ** offset 电流(pA);百分比档需要 fsr 参与计算。
 * 🔴 仅供"盖不盖得住信号"的粗判 —— 换算必须用实测值,见上方注释。
 */
int32_t max30131_offset_pa(max30131_offset_sel_t sel, max30131_fsr_t fsr);

/*
 * 该档 offset 电流的 **min/max** 边界(pA)。用途:把"标称不可信"变成可断言的数字——
 * 实测值落在 [min,max] 内 ⇒ 器件正常;落在外 ⇒ 才该怀疑漏电或配置错。
 */
void max30131_offset_range_pa(max30131_offset_sel_t sel, max30131_fsr_t fsr,
			      int32_t *min_pa, int32_t *max_pa);

/* ================================================================== */
/* 可测量程 与 饱和判定                                                 */
/* ================================================================== */
/*
 * 🔴 offset 电流同时是**还原方向的量程上限**(datasheet p41 原文):
 *   "If the current flowing into the WEn pin is greater than the offset current,
 *    the WEn pin rises above the voltage that is set by the DAC."
 *   ⇒ 还原电流 > offset 时,WE 被顶出设定电位、**失去恒电位控制**,数据整段作废。
 *
 * 两个方向的可测上限(用**实测/typ 的 offset**,不是标称):
 *   还原(电流流入 WE,counts 减小)上限 = offset
 *   氧化(电流流出 WE,counts 增大)上限 = FSR − offset
 */
int32_t max30131_max_reduction_pa(int32_t offset_pa);
int32_t max30131_max_oxidation_pa(int32_t fsr_pa, int32_t offset_pa);

/* 饱和标志位(行协议 sat 字段) */
#define MAX30131_SAT_LOW 0x01u  /* counts 逼近 0:还原电流吃光 offset,失恒电位控制 */
#define MAX30131_SAT_HIGH 0x02u /* counts 逼近满量程:氧化方向超出 FSR−offset */

/*
 * 判定单个样本是否接近饱和。margin_counts = 预警余量(离边界多少 counts 就报)。
 * 🔴 为什么必须报:饱和时读数不是"偏大/偏小",而是**物理上不再是测量**
 *   (恒电位环已开环)。若不标记,上位机会把一段废数据当真数据去算 σ 与标定曲线。
 */
uint8_t max30131_saturation_flags(uint16_t counts, uint16_t margin_counts);

/*
 * 🔴 还原方向(电流流入 WE)是 datasheet 的非原生方向:offset 必须 ≥ 信号峰值,
 * 否则 WEn 会被顶出 DAC 设定的电位、失去恒电位控制并钳在 0nA。
 * 本函数把这条硬约束变成可测断言。margin_pct = 要求的额外余量(如 100 = 留一倍)。
 */
max30131_err_t max30131_check_offset_covers_signal(int32_t offset_pa,
						   int32_t signal_peak_pa,
						   int32_t margin_pct);

/* ================================================================== */
/* counts ↔ 电流                                                       */
/* ================================================================== */
/*
 * datasheet 原式(p39):I_Sn = counts×FSR/2^16 − SnOFFSET
 * 该式的正方向 = 电流「流出 WE」(氧化)。返回值可正可负。
 */
int32_t max30131_counts_to_iwe_pa(uint16_t counts, int32_t fsr_pa,
				  int32_t offset_pa);

/*
 * 还原电流(流入 WE)口径 = −I_WE = offset − counts×FSR/2^16。
 * counts 随还原电流增大而**减小**。本设计(05 文档 §1)用这个口径上报。
 */
int32_t max30131_counts_to_reduction_pa(uint16_t counts, int32_t fsr_pa,
					int32_t offset_pa);

/* 反向:给定还原电流,预期 counts(用于自测与合成数据;超范围时钳位) */
uint16_t max30131_reduction_pa_to_counts(int32_t reduction_pa, int32_t fsr_pa,
					 int32_t offset_pa);

/*
 * 🔴 上报用的 **fA** 口径 —— 固件发到 RTT 行协议的就是这个值。
 * 为什么不用 pA:50nA 档 1 LSB = 763 fA,若按整数 pA 上报,协议本身比器件还粗,
 * 会把亚 pA 噪声量化掉(端到端实测:σ 从 0.42 虚高到 0.50 pA)。
 * 全程 int64 中间量,再收窄到 int32:50nA 档满量程 = 5e7 fA,远在 int32 内。
 * 入参仍用 pA(与 fsr_pa/offset_pa 一致),内部 ×1000。
 */
int32_t max30131_counts_to_reduction_fa(uint16_t counts, int32_t fsr_pa,
					int32_t offset_pa);

/* ================================================================== */
/* 基准 / DAC                                                          */
/* ================================================================== */
typedef enum {
	MAX30131_REF_1536MV = 0, /* POR 默认;CR2032 到 2.0V EOL 只能用这档 */
	MAX30131_REF_2048MV = 1,
	MAX30131_REF_3072MV = 2,
	MAX30131_REF_4096MV = 3,
} max30131_ref_val_t;

int32_t max30131_ref_mv(max30131_ref_val_t ref);

/* VDD 必须比所选基准高 ≥150mV(datasheet p31) */
#define MAX30131_VDD_OVER_REF_MIN_MV 150
max30131_err_t max30131_check_ref_vs_vdd(max30131_ref_val_t ref, int32_t vdd_mv);

/* CODE = round(mV × 4096 / VREF);超 4095 或落进 <19 LSB 非线性区则报错 */
max30131_err_t max30131_dac_code_from_mv(int32_t mv, int32_t vref_mv,
					 uint16_t *code_out);
int32_t max30131_dac_mv_from_code(uint16_t code, int32_t vref_mv);

/* ================================================================== */
/* 极化(恒电位仪)                                                     */
/* ================================================================== */
/*
 * 拓扑(05 文档 §4,3 角度 CONFIRMED):
 *   W AMP 把 WE 钳到 DACA → V_WE = V_DACA
 *   C AMP 驱动 CE 使 **RE** 稳定在 DACB → V_RE = V_DACB
 *   E = V_WE − V_RE = V_DACA − V_DACB
 * 所以 V_DACB = V_DACA − E。E<0(还原)⇒ V_DACB > V_DACA。
 * ⚠️ DACB 设的是 V_RE,CE 引脚是环路自适应输出,不要拿 DACB 去核 CE 摆幅。
 */
typedef struct {
	int32_t v_dac_a_mv; /* = V_WE */
	int32_t v_dac_b_mv; /* = V_RE */
	uint16_t code_a;
	uint16_t code_b;
} max30131_polarization_t;

max30131_err_t max30131_polarization_from_e(int32_t v_we_mv, int32_t e_mv,
					    int32_t vref_mv,
					    max30131_polarization_t *out);

/* WEn(以及 REn)必须 ≤ VDD − 1.1V(CP_EN=0 时);EOL VDD=2.0V ⇒ ≤0.9V */
#define MAX30131_WE_HEADROOM_MV 1100
int32_t max30131_we_max_mv(int32_t vdd_mv);
max30131_err_t max30131_check_headroom(const max30131_polarization_t *p,
				       int32_t vdd_mv);

/* ================================================================== */
/* FIFO                                                                */
/* ================================================================== */
typedef struct {
	bool auto_mode;   /* bit20:1=AUTO 模式转换,0=手动 */
	uint8_t tag;      /* 4-bit tag(≤0xC)或 8-bit tag(>0xC) */
	bool tag_is_8bit; /* true ⇒ counts 只有 12 位 */
	uint16_t counts;
} max30131_fifo_word_t;

/* 解 3 字节突发读(byte[0] 先出;仅取低 21 位) */
max30131_err_t max30131_fifo_unpack(const uint8_t bytes[3],
				    max30131_fifo_word_t *out);

/* 便捷:解一个「Sensor 1 DC 电流」样本并直接给还原电流(pA) */
max30131_err_t max30131_fifo_read_s1_reduction_pa(const uint8_t bytes[3],
						  int32_t fsr_pa,
						  int32_t offset_pa,
						  int32_t *pa_out);

/*
 * 🔴 FIFO_A_FULL 语义是「中断前还剩几个空位」,不是「攒够几个样本」:
 *   A_FULL 置位时 FIFO 内样本数 = 256 − FIFO_A_FULL。
 * 想每 N 个样本醒一次 ⇒ 必须写 256 − N。
 * (docs/ver4.0/05-IC应用设计/U1 §「转换/FIFO/中断」把这条写反了:那里说
 *  「取小值,如 16」,实际写 16 会变成 240 个样本 ≈ 15 分钟一批。待回灌修正。)
 */
uint8_t max30131_fifo_a_full_from_batch(uint16_t samples_per_batch);
uint16_t max30131_fifo_batch_from_a_full(uint8_t a_full);

/* 可读样本数(datasheet p71 伪码):ovf≠0 ⇒ 已丢数,按满 256 处理 */
uint16_t max30131_fifo_available(uint8_t ovf_counter, uint16_t data_count);

/* ================================================================== */
/* 时序表                                                              */
/* ================================================================== */
/* 单次转换时间(ms)。clk_sel: false=34.952kHz, true=40.96kHz */
int32_t max30131_conv_time_ms(uint8_t conv_time_code, bool clk_sel_40k,
			      bool fast_clock_group);
/* 该 conv_time 码的有效分辨率(位) */
uint8_t max30131_conv_time_bits(uint8_t conv_time_code);
/* 自主模式采样周期(ms);SENS_PERIOD 表恒用慢钟,与 FSR 分组无关 */
int32_t max30131_sens_period_ms(uint8_t sens_period_code, bool clk_sel_40k);

/* 🔴 转换时间必须 ≤ 采样周期,否则 STATUS1.INVALID_CFG 置位 */
max30131_err_t max30131_check_period_vs_conv(uint8_t conv_time_code,
					     uint8_t sens_period_code,
					     bool clk_sel_40k,
					     max30131_fsr_t fsr);

/* ================================================================== */
/* 时钟数口径(取代 ms 相除)                                          */
/* ================================================================== */
/*
 * 🔴 为什么必须有这一组:ms 表在同码时会**掩盖真值**。
 * conv 0x0 = 124.20ms、period 0x0 = 124.49ms,两者都四舍五入成 124,
 * `124 ≤ 124` 靠舍入方向侥幸通过;而 idle 窗口用 ms 相除会算出 0,
 * 真值其实是 10 个时钟。校验与占比一律用时钟数,ms 只用于显示。
 *
 * 逐码验证过的恒等式(11 个码全对,见单测 test_conv_clocks_match_ms_tables):
 *   N(code)            = 2^(12+code) − 1        ← 计数器深度,**不**等于 2^bits−1
 *                                                 (code>4 时输出被 decimate 到 16 位,
 *                                                  但计数器继续翻倍,所以时间继续涨)
 *   conv_clocks(code)  = N + 246                ← 246 = precharge(p98 正文)
 *   period_clocks(code)= conv_clocks(code) + 10 ← 同码时 idle 窗口恒为 10 个时钟
 * 计数器上限 8,388,607 = 2^23 − 1 ⇒ 码 ≥0xB 全部夹在该值。
 */
uint32_t max30131_conv_time_clocks(uint8_t conv_time_code);
uint32_t max30131_period_clocks(uint8_t sens_period_code);
/*
 * 一个采样周期里 ADC **没在转换**的时间占比(ppm)。
 * 🔴 这不是"浪费的时间"那么简单:p40 说不转换时 WE 被切到固定 50nA 粗偏置通路
 * (S1.n→VDD、S2.n 闭),而轮次间几十秒的该状态实测会把电极推正、造成下一轮
 * +385~474nA 还原冲击。周期内的几十 ms 是否同样有害尚未实测,但**必须可见**。
 * ⚠️ 两个时钟基不同:conv 随 FSR 分组(快钟组 ×4),period 恒用基频。
 * 实例:FSR 1µA + conv 0x1 + period 0x0 ⇒ 515000 ppm(51.5%),而**同码**只有 2298 ppm。
 */
int32_t max30131_idle_window_ppm(uint8_t conv_time_code, uint8_t sens_period_code,
				 max30131_fsr_t fsr);

/*
 * 积分窗对 50Hz 的抑制(dB×10,负值)。积分窗就是抗混叠滤波器,抑制 = |sinc(f·T)|,
 * 零点在 T = k/f。
 *   *_db_x10       标称时钟下的值
 *   *_worst_db_x10 在 datasheet 给的采样时钟 ±2%(EC 表 f_SLOW)内取最坏
 * 🔴 必须看最坏值:60.35ms 恰在 sinc 零点上(3.018 个工频周期),标称 −44.7dB,
 * 但 ±2% 一偏就塌到 −32dB;而 119ms 靠 sinc 包络,最坏 −31.1dB。
 * ⇒ 标称差 3dB、最坏几乎相同 ⇒ 选码不能只看标称。
 */
int16_t max30131_rej50_db_x10(uint8_t conv_time_code, bool clk_sel_40k,
			      bool fast_clock_group);
int16_t max30131_rej50_worst_db_x10(uint8_t conv_time_code, bool clk_sel_40k,
				    bool fast_clock_group);

/*
 * 给定 FSR/period/时钟源,自动选最优 CONV_TIME 码。
 * 排序键(字典序):① 最坏 50Hz 抑制 ② 位数 ③ idle 窗口小。
 * 🔴 这是对 2026-08-09「写死 CONV_TIME=0x1」那次回归的**结构性**修复:
 * 把耦合关系从宏变成可单测、可打印、可给出备选的决策。
 * 返回选中的码;`alt_out` 非 NULL 时给次优码(审计行用,让"为什么选它"可查)。
 * 无可用码(period 比最短转换还短)返回 -1。
 */
int max30131_auto_conv_code(max30131_fsr_t fsr, uint8_t sens_period_code,
			    bool clk_sel_40k, int *alt_out);

/*
 * System ADC 采完所选通道需要的总时间(ms)。
 * 🔴 p143 硬约束:总时间 > SYS_PERIOD ⇒ 置 INVALID_CFG,且
 * "the conversion cycle abruptly restarts before completing all selected channels.
 *  Data saved in the FIFO is invalid for the interrupted channel."
 * sys_conv_type=false(0x80 bit4=0)⇒ 每通道 offset+signal 两次转换;true ⇒ 每类别共享一次 offset。
 */
int32_t max30131_sysadc_budget_ms(uint8_t n_channels, bool sys_conv_type);

/*
 * 改 E 要写 DACA+DACB 两对寄存器,中间态是"一新一旧"的组合,可能违反
 * headroom(WEn ≤ VDD−1.1V)。返回安全的写序:0=先 A,1=先 B,
 * -1=两个中间态都不安全(此时应分两步经过一个中间电位)。
 */
int max30131_polarization_write_order(const max30131_polarization_t *old_p,
				      const max30131_polarization_t *new_p,
				      int32_t vdd_mv, int32_t vref_mv);

/* ================================================================== */
/* 寄存器字节编码器(让 05 文档里的 hex 变成可断言的表达式)             */
/* ================================================================== */
typedef struct {
	bool we_amp_en;
	bool ce_amp_en;
	uint8_t we_dac_mx; /* MAX30131_DAC_MX_x */
	uint8_t ce_dac_mx;
	bool cp_en;
	bool chop_en;
} max30131_s1_config1_t;
uint8_t max30131_enc_s1_config1(const max30131_s1_config1_t *c);

typedef struct {
	bool swa, swb, sc, sra, srb, ilim_en, rs, swo;
} max30131_s1_config2_t;
uint8_t max30131_enc_s1_config2(const max30131_s1_config2_t *c);
/* Table 1「3-terminal WE drive」的开关组合(本设计用) */
void max30131_switches_3term_we_drive(max30131_s1_config2_t *out);

uint8_t max30131_enc_s1_config3(bool ios_mode, bool detector_en);
uint8_t max30131_enc_s1_config4(max30131_fsr_t fsr, max30131_offset_sel_t off);
uint8_t max30131_enc_s1_config5(uint8_t conv_time_code, bool select);

/*
 * System ADC(只用来量 WE1 引脚电压)。
 * enc_sys_adc_setup 固定把 OPA_BYPASS_EN 写 0 —— 置 1 会引入 14MΩ 负载(≈29nA 漏电),
 * 足以破坏被测对象本身,所以不给调用方留出错的机会。
 */
uint8_t max30131_enc_sys_adc_setup(uint8_t sensv_gain_code);
/* 12-bit 单端:V = code/4096 × VREF / gain。gain_code 见 MAX30131_SYSADC_GAIN_*。 */
int32_t max30131_sys_adc_mv(uint16_t code, int32_t vref_mv, uint8_t gain_code);
uint8_t max30131_enc_reference_control(max30131_ref_val_t ref, bool ref_en,
				       bool external);
uint8_t max30131_enc_convert_setup1(bool eis, uint8_t ioffset_conv,
				    bool sys_conv, uint8_t sens_period_code);
uint8_t max30131_enc_convert_start(bool auto_mode, bool convert);
uint8_t max30131_enc_fifo_config2(bool flush, bool stat_clr, bool a_full_type,
				  bool ro);
uint8_t max30131_enc_int_enable1(bool a_full_en, bool data_rdy_en);
uint8_t max30131_enc_system_control(bool reset, bool shdn, bool bypass_ldo,
				    bool clk_sel_40k);
/*
 * DAC 两字节编码/解码。🔴 布局见 max30131_regs.h:
 *   msb    (0x69/0x6B) = CODE[11:4]
 *   en_lsb (0x6A/0x6C) = CODE[3:0]<<4 | EN(bit0)
 */
void max30131_enc_dac(uint16_t code, bool enable, uint8_t *msb, uint8_t *en_lsb);
uint16_t max30131_dec_dac_code(uint8_t msb, uint8_t en_lsb);

/* ================================================================== */
/* 手动增益校准(datasheet p41;50nA 档 ±2% → ±1% 级)                  */
/* ================================================================== */
/* 步1:在已校准的参考档(500nA)做 offset-only 转换 → offset 真值 */
int32_t max30131_cal_ioffset_pa(uint16_t adc_at_ref_range, int32_t ref_fsr_pa);
/* 步2:在目标档(50nA)做 offset-only 转换 → 该档 FSR 真值 */
int32_t max30131_cal_fsr_pa(int32_t ioffset_pa, uint16_t adc_at_target_range);

/*
 * ★★ 差分换算 —— **标定后测量应当用这个,而不是绝对 offset 版** ★★
 *
 * 还原电流 = (offset-only 读数 − 信号读数) × FSR / 2¹⁶
 *
 * 🔴 为什么它比 max30131_counts_to_reduction_fa() 更准:**ADC 自身的 offset 误差被减掉了**。
 *   设 ADC 有固有偏移 e(50nA 档规格 ±80 LSB = ±61 pA):
 *      读数_offset_only = (I_off      + e) × 2¹⁶/FSR
 *      读数_signal      = (I_off − I  + e) × 2¹⁶/FSR      (还原电流 I 使 counts 减小)
 *      两者相减         =  I × 2¹⁶/FSR                    ← **e 精确消掉**
 *   同理 offset 电流源的绝对容差(±22%)也一并消掉 —— 因为它在两次读数里都一样。
 *   ⇒ 标定后残余误差只剩 **FSR 增益精度**(双档反算后约 ±1%,继承 500nA 档出厂校准)
 *      与噪声,不再包含 ±61pA 的 ADC 偏移和 ±2nA 的 offset 源容差。
 *
 * ⚠️ 前提:两次读数必须**同一量程档、同一 offset 档**,且间隔内温度/VDD 未显著变化
 *   (offset 漂移 0.4 LSB/°C、增益漂移 20 LSB/°C@0.9FS ⇒ 隔得越近越好)。
 *   datasheet 建议:offset 漂移很慢,偶尔重测一次 offset-only 即可。
 *
 * 单位:返回 fA(与行协议一致,保住亚 pA 分辨率);fsr_pa 传**标定后的真值**。
 */
int32_t max30131_reduction_from_counts_diff_fa(uint16_t counts_offset_only,
					       uint16_t counts_signal,
					       int32_t fsr_pa);

#ifdef __cplusplus
}
#endif

#endif /* MAX30131_H */
