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
#define WP_FSR        GUI_WP_FSR
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
#define WP_OFFSET_SEL GUI_WP_OFFSET_SEL
#define WP_REF        MAX30131_REF_1536MV      /* 内部 1.536V;CR2032 EOL 2.0V 只此档 */

#define WP_V_WE_MV    400                      /* WE 电位 0.4V */
#define WP_E_MV       GUI_WP_E_MV              /* E = V_WE - V_RE */
#define WP_STARTUP_E_MV GUI_WP_START_E_MV      /* 用户可见的阶跃起始电位 */
#define WP_PRESTEP_DURATION_MS GUI_PRESTEP_DURATION_MS
#define WP_RUN_STARTUP_DIAGNOSTIC false         /* i-t 正式测量不扫其他电位 */
#define WP_RUN_AFE_GAIN_CALIBRATION false       /* 10Hz 模式由上位机做浓度标定;不阻塞采样 */

/* ADC 时钟源:false = 34.952kHz(慢钟),true = 40.96kHz。 */
#define WP_CLK_40K    false

#define WP_CONV_TIME_CODE   0x0U               /* 31ms / 12 位(1000nA 快速档) */
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

/* 饱和预警余量(counts)。2% FS —— 离边界还有 1311 counts 就开始报,给上位机反应余地。 */
#define SAT_MARGIN_COUNTS 1311U

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
		{ 0x22U, 0x08U, "S1_CONFIG3(critic 修正,原 00 且语义反)" },
		{ 0x23U, max30131_enc_s1_config4(WP_FSR, WP_OFFSET_SEL),
		  "S1_CONFIG4: configured FSR/offset" },
		{ 0x24U, 0x01U, "S1_CONFIG5: CONV_TIME=0x0(12bit)" },
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
	LOG_INF("AFE 就绪:FSR=%d pA / offset=%d pA / LSB=%d fA", fsr_pa, off_pa,
		max30131_lsb_fa(WP_FSR));
	/* 🔴 把两个方向的可测上限显式打出来 —— 别让"量程够不够"停留在文档里 */
	int32_t off_lo = 0, off_hi = 0;

	max30131_offset_range_pa(WP_OFFSET_SEL, WP_FSR, &off_lo, &off_hi);
	LOG_INF("可测量程:还原 ≤%d pA(=offset,最坏 min 档只有 %d pA)/ 氧化 ≤%d pA",
		max30131_max_reduction_pa(off_pa), max30131_max_reduction_pa(off_lo),
		max30131_max_oxidation_pa(fsr_pa, off_pa));
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
static void wait_for_start_command(void)
{
	char command[16];
	size_t used = 0U;

	printk("IT_READY target_mv=%d\n", WP_E_MV);
	while (1) {
		char ch;

		board_guards_feed();
		while (SEGGER_RTT_Read(0U, &ch, 1U) == 1U) {
			if (ch == '\r' || ch == '\n') {
				command[used] = '\0';
				if (strcmp(command, "START") == 0) {
					return;
				}
				used = 0U;
				continue;
			}
			if (used + 1U < sizeof(command)) {
				command[used++] = ch;
			} else {
				used = 0U;
			}
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
		uint8_t sat = max30131_saturation_flags(s.counts, SAT_MARGIN_COUNTS);

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
	while (1) {
		wait_for_start_command();
		run_number++;
		last_sat = 0U;
		printk("IT_START run=%u target_mv=%d\n", run_number, WP_E_MV);

	if (WP_PRESTEP_DURATION_MS > 0U) {
		LOG_INF("IT 阶跃前保持:E=%d mV,hold=%u ms", WP_STARTUP_E_MV,
			WP_PRESTEP_DURATION_MS);
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
	LOG_INF("进入 AUTO i-t 采集: %u native samples (约8Hz; host重采样10Hz), E=%d mV",
		WP_EXPECTED_SAMPLE_COUNT, WP_E_MV);

	while (native_samples < WP_EXPECTED_SAMPLE_COUNT) {
		board_guards_feed();
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

	/* 停止任何残留转换并把电位恢复为用户配置的起始值。 */
	(void)max30131_spi_write_reg(MAX30131_REG_CONVERT_START,
					 max30131_enc_convert_start(false, false));
	(void)set_polarization(WP_STARTUP_E_MV);
	LOG_INF("i-t 测量结束:elapsed=%lld ms,native=%u/%u,empty polls=%u,E 已恢复为 %d mV",
		(long long)(k_uptime_get() - measurement_start_ms), native_samples,
		WP_EXPECTED_SAMPLE_COUNT, conversion_errors, WP_STARTUP_E_MV);
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
