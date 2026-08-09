/*
 * test_max30131.c — MAX30131 纯逻辑层单测
 *
 * 用途    : 把「回板当天才会暴露、且暴露时最贵」的东西现在就钉死:
 *           寄存器 hex 必须等于 05 文档定版值、FIFO 位序与 tag 分支、
 *           counts↔电流方向与舍入、DAC 两字节布局、极化符号、
 *           共模余量、FIFO watermark 反语义、时序表与 INVALID_CFG 约束、
 *           手动增益校准反算。
 * 用法    : cd software/firmware/tests && make test
 * 前置条件: clang 或 gcc(C99)。无需 Zephyr / NCS / 硬件。
 * 快照日期: 2026-07-27
 *
 * 每条断言的期望值都注明来源:
 *   [05]  = docs/ver4.0/05-IC应用设计/U1-MAX30131-AFE应用.md(经 critic 定版)
 *   [ds]  = datasheet(章节见 max30131_regs.h 头部)
 */

#include "../lib/max30131/max30131.h"
#include "minitest.h"

#define ARRAY_SIZE_LOCAL(a) (sizeof(a) / sizeof((a)[0]))

/* 本设计工作点(与 [05] §0 一致) */
#define WP_FSR MAX30131_FSR_50NA
#define WP_OFFSET_SEL MAX30131_OFFSET_SEL4_9NA
#define WP_CONV_TIME 0x4u
#define WP_SENS_PERIOD 0x5u
#define WP_CLK_40K false /* CLK_SEL=0 → 34.952kHz */
#define WP_VREF_MV 1536
#define WP_V_WE_MV 400
#define WP_E_MV (-200)

/* ================================================================== */
/* 1. 寄存器 hex 必须复现 [05] 的定版值                                */
/* ================================================================== */
TEST(test_reg_hex_matches_design_doc)
{
	max30131_s1_config1_t c1 = {
		.we_amp_en = true,
		.ce_amp_en = true,
		.we_dac_mx = MAX30131_DAC_MX_A,
		.ce_dac_mx = MAX30131_DAC_MX_B, /* 🔴 必须显式 01,否则 E=0 */
		.cp_en = false,
		.chop_en = true,
	};
	max30131_s1_config2_t c2;

	CHECK_EQ(max30131_enc_s1_config1(&c1), 0xC5); /* [05] 0x20 */

	max30131_switches_3term_we_drive(&c2);
	CHECK_EQ(max30131_enc_s1_config2(&c2), 0x90); /* [05] 0x21 */

	CHECK_EQ(max30131_enc_s1_config3(true, false), 0x08); /* [05] 0x22 */
	CHECK_EQ(max30131_enc_s1_config4(WP_FSR, WP_OFFSET_SEL),
		 0x04); /* [05] 0x23 */
	CHECK_EQ(max30131_enc_s1_config5(WP_CONV_TIME, true),
		 0x09); /* [05] 0x24 */
	CHECK_EQ(max30131_enc_reference_control(MAX30131_REF_1536MV, true,
						false),
		 0x01); /* [05] 0x68 */
	CHECK_EQ(max30131_enc_convert_setup1(false, 0u, false, WP_SENS_PERIOD),
		 0x05); /* [05] 0x80 */
	CHECK_EQ(max30131_enc_convert_start(true, true), 0x03); /* [05] 0x83 */
	CHECK_EQ(max30131_enc_int_enable1(true, false), 0x80); /* [05] 0x05 */

	/* FIFO_CONFIG2:批读用 A_FULL_TYPE=1(不每样本重触发)+ STAT_CLR=1;
	 * FIFO_RO=0 = 满了停止推入(丢新样本但 OVF_COUNTER 能如实报告丢了多少) */
	CHECK_EQ(max30131_enc_fifo_config2(false, true, true, false), 0x0C);

	/* 软复位序列的两个字节 */
	CHECK_EQ(max30131_enc_system_control(true, false, false, WP_CLK_40K),
		 0x01);
	CHECK_EQ(max30131_enc_system_control(false, false, false, WP_CLK_40K),
		 0x00);
}

/* 反例:忘了写 CE_DAC_MX(复位默认 00)→ 两放大器共用 DACA → E 恒为 0 */
TEST(test_ce_dac_mx_default_would_give_zero_e)
{
	max30131_s1_config1_t bad = {
		.we_amp_en = true,
		.ce_amp_en = true,
		.we_dac_mx = MAX30131_DAC_MX_A,
		.ce_dac_mx = MAX30131_DAC_MX_A, /* 忘改 */
		.cp_en = false,
		.chop_en = true,
	};

	CHECK_EQ(max30131_enc_s1_config1(&bad), 0xC1); /* ≠0xC5,能被 diff 出来 */
}

/* ================================================================== */
/* 2. FSR / offset 码表(含 250nA 勘误)                               */
/* ================================================================== */
TEST(test_fsr_table)
{
	CHECK_EQ(max30131_fsr_pa(MAX30131_FSR_50NA), 50000);
	CHECK_EQ(max30131_fsr_pa(MAX30131_FSR_100NA), 100000);
	CHECK_EQ(max30131_fsr_pa(MAX30131_FSR_250NA), 250000); /* 不是 200000 */
	CHECK_EQ(max30131_fsr_pa(MAX30131_FSR_500NA), 500000);
	CHECK_EQ(max30131_fsr_pa(MAX30131_FSR_1000NA), 1000000);
	CHECK_EQ(max30131_fsr_pa(MAX30131_FSR_2000NA), 2000000);
	CHECK_EQ(max30131_fsr_pa((max30131_fsr_t)6), 0); /* Reserved */
	CHECK_EQ(max30131_fsr_pa((max30131_fsr_t)7), 0);

	/* [ds] FSR 码 ≤3 走慢钟,>3 全通道 4× 钟 */
	CHECK_FALSE(max30131_fsr_uses_fast_clock(MAX30131_FSR_500NA));
	CHECK_TRUE(max30131_fsr_uses_fast_clock(MAX30131_FSR_1000NA));

	/* [05] 50nA 档 LSB = 0.763pA */
	CHECK_EQ(max30131_lsb_fa(MAX30131_FSR_50NA), 763);
}

TEST(test_offset_table)
{
	/*
	 * 绝对档 —— 🔴 用 datasheet 的 **typ**,不是整数标称值。
	 * SEL4 typ 9nA(不是 10)、SEL5 typ 19nA(不是 20);SEL6/7 恰好是整数。
	 * 2026-08-01 逐行核 datasheet「Offset Current」表后更正。
	 */
	CHECK_EQ(max30131_offset_pa(MAX30131_OFFSET_SEL4_9NA, WP_FSR), 9000);
	CHECK_EQ(max30131_offset_pa(MAX30131_OFFSET_SEL5_19NA, WP_FSR), 19000);
	CHECK_EQ(max30131_offset_pa(MAX30131_OFFSET_SEL6_40NA, WP_FSR), 40000);
	CHECK_EQ(max30131_offset_pa(MAX30131_OFFSET_SEL7_80NA, WP_FSR), 80000);
	/* 百分比档随 FSR 变 */
	CHECK_EQ(max30131_offset_pa(MAX30131_OFFSET_10PCT_FSR, WP_FSR), 5000);
	CHECK_EQ(max30131_offset_pa(MAX30131_OFFSET_20PCT_FSR, WP_FSR), 10000);
	CHECK_EQ(max30131_offset_pa(MAX30131_OFFSET_50PCT_FSR, WP_FSR), 25000);
	CHECK_EQ(max30131_offset_pa(MAX30131_OFFSET_20PCT_FSR,
				    MAX30131_FSR_500NA),
		 100000);
	CHECK_EQ(max30131_offset_pa(MAX30131_OFFSET_0PCT, WP_FSR), 0);
}

/*
 * 🔴 容差边界:datasheet 的 offset 电流带 ±20% 级容差,typ **不能**用于换算。
 * 本测把边界变成可断言的数字 —— 实测值落在 [min,max] 内即器件正常,
 * 落在外才该怀疑漏电或配置错(2026-07-31 实测 8780pA,落在 7000–11000 内 ⇒ 正常)。
 */
TEST(test_offset_tolerance_bounds)
{
	int32_t lo = 0, hi = 0;

	max30131_offset_range_pa(MAX30131_OFFSET_SEL4_9NA, WP_FSR, &lo, &hi);
	CHECK_EQ(lo, 7000);  /* [ds] min 7nA */
	CHECK_EQ(hi, 11000); /* [ds] max 11nA */
	/* 实测 8780pA 必须被判为正常 */
	CHECK_TRUE(8780 >= lo && 8780 <= hi);
	/* typ 必须在区间内 */
	int32_t typ = max30131_offset_pa(MAX30131_OFFSET_SEL4_9NA, WP_FSR);

	CHECK_TRUE(typ >= lo && typ <= hi);

	max30131_offset_range_pa(MAX30131_OFFSET_SEL5_19NA, WP_FSR, &lo, &hi);
	CHECK_EQ(lo, 16000);
	CHECK_EQ(hi, 22000);

	/* 百分比档随 FSR 缩放(9–11 %FS) */
	max30131_offset_range_pa(MAX30131_OFFSET_10PCT_FSR, WP_FSR, &lo, &hi);
	CHECK_EQ(lo, 4500);
	CHECK_EQ(hi, 5500);
}

/* 🔴 还原方向下 offset 必须 ≥ 信号峰值,否则 WE 失恒电位控制 */
TEST(test_offset_must_cover_signal_peak)
{
	int32_t off = max30131_offset_pa(WP_OFFSET_SEL, WP_FSR); /* typ 9000pA */
	int32_t off_min = 0;

	max30131_offset_range_pa(WP_OFFSET_SEL, WP_FSR, &off_min, NULL);

	/*
	 * 🔴🔴 2026-08-01 发现的设计问题(改 typ 值后暴露):
	 * [05] 文档写「offset 固定 10nA,覆盖 5nA 信号峰」并按**留一倍余量**论证。
	 * 但实际 typ 只有 **9nA** ⇒ 余量 1.8× 而非 2×;**最坏 min 7nA ⇒ 只剩 1.4×**。
	 * 而 offset 不够时后果是硬的:WEn 被顶出 DAC 设定电位、**失去恒电位控制并钳在 0nA**。
	 * ⇒ 是否升到 SEL5(typ 19nA/min 16nA,余量 3.2×)待拍板 —— 代价是热漂:
	 *   基线随增益漂移 ≈ offset × 0.034%/°C,9nA→3.1pA/°C,19nA→6.5pA/°C(翻倍)。
	 */
	CHECK_EQ(off, 9000);

	/* typ 下:5nA 信号 + 100% 余量已经**不够**了(需 10nA > 9nA) */
	CHECK_EQ(max30131_check_offset_covers_signal(off, 5000, 100),
		 MAX30131_ERR_RANGE);
	/* typ 下最多支持 80% 余量 */
	CHECK_EQ(max30131_check_offset_covers_signal(off, 5000, 80),
		 MAX30131_OK);
	/* 🔴 最坏 min=7nA 时,5nA 信号连 40% 余量都紧 */
	CHECK_EQ(max30131_check_offset_covers_signal(off_min, 5000, 40),
		 MAX30131_OK);
	CHECK_EQ(max30131_check_offset_covers_signal(off_min, 5000, 41),
		 MAX30131_ERR_RANGE);
	/* 不留余量时 typ 9nA 撑到 9nA 信号 */
	CHECK_EQ(max30131_check_offset_covers_signal(off, 9000, 0), MAX30131_OK);
	CHECK_EQ(max30131_check_offset_covers_signal(off, 9001, 0),
		 MAX30131_ERR_RANGE);

	/* 若升到 SEL5:5nA 信号 + 100% 余量轻松通过,最坏 min 也够 */
	int32_t off5 = max30131_offset_pa(MAX30131_OFFSET_SEL5_19NA, WP_FSR);
	int32_t off5_min = 0;

	max30131_offset_range_pa(MAX30131_OFFSET_SEL5_19NA, WP_FSR, &off5_min, NULL);
	CHECK_EQ(max30131_check_offset_covers_signal(off5, 5000, 100), MAX30131_OK);
	CHECK_EQ(max30131_check_offset_covers_signal(off5_min, 5000, 100),
		 MAX30131_OK);
}

/*
 * ★★ 真实标定数据的回归断言 ★★
 * 数据来源:docs/左旋多巴标定/ 的 CHI660E i-t 曲线(E=−0.2V,与本设计工作点相同),
 * 稳态电流 6.07 / 7.23 / 9.15 / 12.80 nA(浓度 6.25 / 12.5 / 25 / 50)。
 *
 * 🔴 这组数字把 offset 档位从"可选优化"变成"硬约束":
 *   还原方向的量程上限 = offset。最大信号 12.80 nA:
 *     SEL4  typ 9nA / min 7nA   → **盖不住**,高浓度点会失恒电位控制、整段钳 0nA
 *     SEL5  typ 19nA / min 16nA → 够(min 也有 1.25× 余量)
 * 谁把工作档位改回 SEL4,本测就会红。
 */
TEST(test_ldopa_calibration_currents_fit_the_range)
{
	const int32_t i_max_pa = 12800; /* 最高浓度稳态 12.80 nA */
	const int32_t fsr = 50000;
	int32_t lo4 = 0, lo5 = 0;

	max30131_offset_range_pa(MAX30131_OFFSET_SEL4_9NA, MAX30131_FSR_50NA, &lo4, NULL);
	max30131_offset_range_pa(MAX30131_OFFSET_SEL5_19NA, MAX30131_FSR_50NA, &lo5, NULL);

	/* 🔴 SEL4:连 typ 都盖不住 12.8nA */
	CHECK_TRUE(max30131_max_reduction_pa(
			   max30131_offset_pa(MAX30131_OFFSET_SEL4_9NA,
					      MAX30131_FSR_50NA)) < i_max_pa);
	/* 最坏 min 7nA 更差 */
	CHECK_TRUE(max30131_max_reduction_pa(lo4) < i_max_pa);

	/* ✅ SEL5:typ 与 min 都够 */
	CHECK_TRUE(max30131_max_reduction_pa(
			   max30131_offset_pa(MAX30131_OFFSET_SEL5_19NA,
					      MAX30131_FSR_50NA)) > i_max_pa);
	CHECK_TRUE(max30131_max_reduction_pa(lo5) > i_max_pa);

	/* SEL5 下氧化方向仍有 50−19 = 31nA 余量 */
	CHECK_EQ(max30131_max_oxidation_pa(fsr, 19000), 31000);

	/*
	 * ⚠️ 瞬态另算:i-t 阶跃后 0.1s 的扩散尖峰达 90nA(Cottrell,i∝t^−1/2),
	 * 连 SEL7(typ 80nA)都盖不住 ⇒ 必须靠饱和标记识别并丢弃,不能指望量程扛。
	 */
	CHECK_TRUE(max30131_max_reduction_pa(
			   max30131_offset_pa(MAX30131_OFFSET_SEL7_80NA,
					      MAX30131_FSR_50NA)) < 90000);
}

TEST(test_saturation_flags)
{
	const uint16_t margin = 1311; /* 2% FS */

	/* 正常区:两个标志都不置 */
	CHECK_EQ(max30131_saturation_flags(24904, margin), 0); /* SEL5 基线 */
	CHECK_EQ(max30131_saturation_flags(32768, margin), 0);

	/* 逼近 0 —— 还原电流吃光 offset */
	CHECK_EQ(max30131_saturation_flags(0, margin), MAX30131_SAT_LOW);
	CHECK_EQ(max30131_saturation_flags(margin, margin), MAX30131_SAT_LOW);
	CHECK_EQ(max30131_saturation_flags(margin + 1, margin), 0); /* 刚好出预警区 */

	/* 逼近满量程 —— 氧化方向超出 FSR−offset */
	CHECK_EQ(max30131_saturation_flags(65535, margin), MAX30131_SAT_HIGH);
	CHECK_EQ(max30131_saturation_flags(65535 - margin, margin),
		 MAX30131_SAT_HIGH);
	CHECK_EQ(max30131_saturation_flags(65535 - margin - 1, margin), 0);

	/* margin=0 时只在真边界报 */
	CHECK_EQ(max30131_saturation_flags(0, 0), MAX30131_SAT_LOW);
	CHECK_EQ(max30131_saturation_flags(1, 0), 0);
	CHECK_EQ(max30131_saturation_flags(65535, 0), MAX30131_SAT_HIGH);

	/* 量程上限换算 */
	CHECK_EQ(max30131_max_reduction_pa(19000), 19000);
	CHECK_EQ(max30131_max_oxidation_pa(50000, 19000), 31000);
	CHECK_EQ(max30131_max_oxidation_pa(50000, 60000), 0); /* 不返回负数 */
	CHECK_EQ(max30131_max_reduction_pa(-1), 0);
}

/* ================================================================== */
/* 3. counts ↔ 电流:方向、舍入、单调性                                */
/* ================================================================== */
TEST(test_reduction_fa_keeps_sub_pa_resolution)
{
	int32_t fsr = max30131_fsr_pa(WP_FSR);                   /* 50000 pA */
	int32_t off = max30131_offset_pa(WP_OFFSET_SEL, WP_FSR); /* typ 9000 pA */

	/* counts=0 → 全 offset,typ 9000 pA = 9,000,000 fA */
	CHECK_EQ(max30131_counts_to_reduction_fa(0, fsr, off), 9000000);

	/*
	 * 🔴 本函数存在的唯一理由:1 LSB 必须能被分辨。
	 * 50nA 档 LSB = 763 fA ⇒ counts 相邻两档的 fA 差应 ≈763,
	 * 而按整数 pA 上报时这个差会被量化成 0 或 1000。
	 */
	int32_t lsb = max30131_lsb_fa(WP_FSR);
	CHECK_EQ(lsb, 763);

	int32_t a = max30131_counts_to_reduction_fa(1000, fsr, off);
	int32_t b = max30131_counts_to_reduction_fa(1001, fsr, off);
	CHECK_NEAR(a - b, lsb, 2); /* 差一个 counts = 差一个 LSB(舍入 ±2 fA) */

	/* 反面:同样两点在 pA 口径下**分辨不出来**(差为 0 或 1) */
	int32_t pa_a = max30131_counts_to_reduction_pa(1000, fsr, off);
	int32_t pa_b = max30131_counts_to_reduction_pa(1001, fsr, off);
	CHECK_TRUE(pa_a - pa_b <= 1);

	/* 与 pA 口径在整 pA 处必须自洽(±1000 fA 内) */
	CHECK_NEAR(max30131_counts_to_reduction_fa(20000, fsr, off),
		   max30131_counts_to_reduction_pa(20000, fsr, off) * 1000, 1000);

	/* 不溢出 int32:满量程 counts=0 且 offset 取最大 80nA 档 */
	int32_t off80 = max30131_offset_pa(MAX30131_OFFSET_SEL7_80NA, WP_FSR);
	CHECK_EQ(max30131_counts_to_reduction_fa(0, fsr, off80), 80000000);
}

TEST(test_counts_to_current)
{
	int32_t fsr = max30131_fsr_pa(WP_FSR);           /* 50000 pA */
	int32_t off = max30131_offset_pa(WP_OFFSET_SEL, WP_FSR); /* typ 9000 pA */

	/* counts=0 → 全 offset 被当成还原电流(typ 9000pA) */
	CHECK_EQ(max30131_counts_to_reduction_pa(0, fsr, off), 9000);
	CHECK_EQ(max30131_counts_to_iwe_pa(0, fsr, off), -9000);

	/* [05] 工作区:还原 0.5nA ⇒ ADC 读 9.5nA;还原 5nA ⇒ 读 5nA */
	CHECK_NEAR(max30131_counts_to_reduction_pa(
			   max30131_reduction_pa_to_counts(500, fsr, off), fsr,
			   off),
		   500, 1);
	CHECK_NEAR(max30131_counts_to_reduction_pa(
			   max30131_reduction_pa_to_counts(5000, fsr, off), fsr,
			   off),
		   5000, 1);

	/* 🔴 方向:counts 增大 ⇒ 还原电流减小(和 I_WE 口径反号) */
	CHECK_TRUE(max30131_counts_to_reduction_pa(20000, fsr, off) >
		   max30131_counts_to_reduction_pa(30000, fsr, off));
	CHECK_TRUE(max30131_counts_to_iwe_pa(20000, fsr, off) <
		   max30131_counts_to_iwe_pa(30000, fsr, off));

	/* 满量程附近不溢出(65535×2000000 会爆 int32,内部必须用 int64) */
	CHECK_NEAR(max30131_counts_to_iwe_pa(
			   65535, max30131_fsr_pa(MAX30131_FSR_2000NA), 0),
		   1999969, 2);

	/* 舍入:1 count @50nA 档 = 763 fA → 0 或 1 pA,不能是 0 恒定 */
	CHECK_EQ(max30131_counts_to_iwe_pa(1, fsr, 0), 1);
	CHECK_EQ(max30131_counts_to_iwe_pa(655, fsr, 0), 500); /* [05] 0.5nA=655 counts */
}

/* ================================================================== */
/* 4. 基准 / DAC / 极化                                                */
/* ================================================================== */
TEST(test_ref_and_vdd)
{
	CHECK_EQ(max30131_ref_mv(MAX30131_REF_1536MV), 1536);
	CHECK_EQ(max30131_ref_mv(MAX30131_REF_4096MV), 4096);

	/* [05] CR2032 到 2.0V EOL:1.536V 档可用(1536+150=1686 ≤ 2000) */
	CHECK_EQ(max30131_check_ref_vs_vdd(MAX30131_REF_1536MV, 2000),
		 MAX30131_OK);
	/* 2.048V 档在 EOL 下不合法 */
	CHECK_EQ(max30131_check_ref_vs_vdd(MAX30131_REF_2048MV, 2000),
		 MAX30131_ERR_HEADROOM);
}

TEST(test_dac_code_and_byte_layout)
{
	uint16_t code = 0;
	uint8_t msb = 0, en_lsb = 0;

	/* [05] 示例三个码值 */
	CHECK_EQ(max30131_dac_code_from_mv(400, WP_VREF_MV, &code),
		 MAX30131_OK);
	CHECK_EQ(code, 0x42B);
	CHECK_EQ(max30131_dac_code_from_mv(500, WP_VREF_MV, &code),
		 MAX30131_OK);
	CHECK_EQ(code, 0x535);
	CHECK_EQ(max30131_dac_code_from_mv(600, WP_VREF_MV, &code),
		 MAX30131_OK);
	CHECK_EQ(code, 0x640);

	/* 往返 */
	CHECK_NEAR(max30131_dac_mv_from_code(0x640, WP_VREF_MV), 600, 1);

	/* 越界与 <19LSB 非线性区都要报错,不能静默钳位 */
	CHECK_EQ(max30131_dac_code_from_mv(1600, WP_VREF_MV, &code),
		 MAX30131_ERR_RANGE);
	CHECK_EQ(max30131_dac_code_from_mv(5, WP_VREF_MV, &code),
		 MAX30131_ERR_RANGE);

	/* 🔴 两字节布局:0x69=CODE[11:4],0x6A=CODE[3:0]<<4|EN(bit0) */
	max30131_enc_dac(0x640, true, &msb, &en_lsb);
	CHECK_EQ(msb, 0x64);
	CHECK_EQ(en_lsb, 0x01);
	max30131_enc_dac(0x42B, true, &msb, &en_lsb);
	CHECK_EQ(msb, 0x42);
	CHECK_EQ(en_lsb, 0xB1);
	max30131_enc_dac(0x42B, false, &msb, &en_lsb);
	CHECK_EQ(en_lsb, 0xB0); /* EN=0 */
	CHECK_EQ(max30131_dec_dac_code(0x42, 0xB1), 0x42B);
}

TEST(test_polarization_sign)
{
	max30131_polarization_t p;

	/* [05] §4:E = V_DACA − V_DACB;E=−200mV & V_WE=400mV ⇒ V_RE=600mV */
	CHECK_EQ(max30131_polarization_from_e(WP_V_WE_MV, WP_E_MV, WP_VREF_MV,
					      &p),
		 MAX30131_OK);
	CHECK_EQ(p.v_dac_a_mv, 400);
	CHECK_EQ(p.v_dac_b_mv, 600);
	CHECK_EQ(p.code_a, 0x42B);
	CHECK_EQ(p.code_b, 0x640);

	/* E=−100mV ⇒ V_RE=500mV → 0x535 */
	CHECK_EQ(max30131_polarization_from_e(400, -100, WP_VREF_MV, &p),
		 MAX30131_OK);
	CHECK_EQ(p.code_b, 0x535);

	/* 正 E(氧化)也应能表达:E=+200mV ⇒ V_RE=200mV < V_WE */
	CHECK_EQ(max30131_polarization_from_e(400, 200, WP_VREF_MV, &p),
		 MAX30131_OK);
	CHECK_EQ(p.v_dac_b_mv, 200);
	CHECK_TRUE(p.code_b < p.code_a);

	/* V_RE 会算成负 ⇒ 单极性 DAC 表达不了,必须报错 */
	CHECK_EQ(max30131_polarization_from_e(100, 200, WP_VREF_MV, &p),
		 MAX30131_ERR_RANGE);
}

TEST(test_headroom)
{
	max30131_polarization_t p;

	/* [05] EOL VDD=2.0V ⇒ WEn/REn 上限 0.9V */
	CHECK_EQ(max30131_we_max_mv(2000), 900);
	CHECK_EQ(max30131_we_max_mv(3000), 1900);

	max30131_polarization_from_e(400, -200, WP_VREF_MV, &p);
	CHECK_EQ(max30131_check_headroom(&p, 2000), MAX30131_OK); /* 600≤900 ✓ */

	/* V_WE 抬到 800mV:E=−200 ⇒ V_RE=1000mV > 900mV,EOL 下越界 */
	max30131_polarization_from_e(800, -200, WP_VREF_MV, &p);
	CHECK_EQ(max30131_check_headroom(&p, 2000), MAX30131_ERR_HEADROOM);
	/* 电池新的时候(3.0V)同一设定合法 → 说明这是 EOL 专属约束 */
	CHECK_EQ(max30131_check_headroom(&p, 3000), MAX30131_OK);
}

/* ================================================================== */
/* 5. FIFO:位序、tag 分支、空标记、watermark 反语义                    */
/* ================================================================== */
TEST(test_fifo_unpack)
{
	max30131_fifo_word_t w;
	uint8_t b[3];

	/* AUTO=1, tag=0x0(S1 DC), counts=0x1234
	 * raw = (1<<20)|(0x0<<16)|0x1234 = 0x101234 */
	b[0] = 0x10;
	b[1] = 0x12;
	b[2] = 0x34;
	CHECK_EQ(max30131_fifo_unpack(b, &w), MAX30131_OK);
	CHECK_TRUE(w.auto_mode);
	CHECK_FALSE(w.tag_is_8bit);
	CHECK_EQ(w.tag, MAX30131_FIFO_TAG_S1_DC);
	CHECK_EQ(w.counts, 0x1234);

	/* 手动模式(AUTO=0),tag=0xC(温度,仍是 4-bit tag 边界) */
	b[0] = 0x0C;
	b[1] = 0xAB;
	b[2] = 0xCD;
	CHECK_EQ(max30131_fifo_unpack(b, &w), MAX30131_OK);
	CHECK_FALSE(w.auto_mode);
	CHECK_FALSE(w.tag_is_8bit);
	CHECK_EQ(w.tag, 0x0C);
	CHECK_EQ(w.counts, 0xABCD);

	/* tag4 > 0xC ⇒ 切 8-bit tag + 12-bit counts。
	 * 取 tag8=0xD1(S1 WE 引脚电压),counts=0x123
	 * raw = (0xD1<<12)|0x123 = 0xD1123;bits[19:16]=0xD > 0xC ✓ */
	b[0] = 0x0D;
	b[1] = 0x11;
	b[2] = 0x23;
	CHECK_EQ(max30131_fifo_unpack(b, &w), MAX30131_OK);
	CHECK_TRUE(w.tag_is_8bit);
	CHECK_EQ(w.tag, 0xD1);
	CHECK_EQ(w.counts, 0x123);

	/* 空 FIFO 哨兵 tag=0xFE:raw = 0xFE<<12 = 0xFE000 */
	b[0] = 0x0F;
	b[1] = 0xE0;
	b[2] = 0x00;
	CHECK_EQ(max30131_fifo_unpack(b, &w), MAX30131_ERR_FIFO_EMPTY);

	/* byte0 的 bit[7:5] 是保留位,必须被屏蔽掉而不是污染 AUTO/tag。
	 * 0xF0 = 0b1111_0000 → 有效低 5 位 = 0b10000 = AUTO=1,tag=0 */
	b[0] = 0xF0;
	b[1] = 0x12;
	b[2] = 0x34;
	CHECK_EQ(max30131_fifo_unpack(b, &w), MAX30131_OK);
	CHECK_TRUE(w.auto_mode);
	CHECK_EQ(w.tag, 0x00);
	CHECK_EQ(w.counts, 0x1234);
}

TEST(test_fifo_read_helper)
{
	int32_t fsr = max30131_fsr_pa(WP_FSR);
	int32_t off = max30131_offset_pa(WP_OFFSET_SEL, WP_FSR);
	int32_t pa = 0;
	uint16_t counts = max30131_reduction_pa_to_counts(2500, fsr, off);
	uint8_t b[3];

	/* 组一个 AUTO=1 / tag=0 / counts=<2.5nA 还原> 的 FIFO 词 */
	b[0] = (uint8_t)(1u << 4); /* AUTO=1, tag=0 */
	b[1] = (uint8_t)(counts >> 8);
	b[2] = (uint8_t)(counts & 0xFFu);
	CHECK_EQ(max30131_fifo_read_s1_reduction_pa(b, fsr, off, &pa),
		 MAX30131_OK);
	CHECK_NEAR(pa, 2500, 1);

	/* 非 S1 DC 的 tag 必须被拒绝,不能当电流用 */
	b[0] = (uint8_t)((1u << 4) | 0x0Cu); /* tag=0xC 温度 */
	CHECK_EQ(max30131_fifo_read_s1_reduction_pa(b, fsr, off, &pa),
		 MAX30131_ERR_FIFO_TAG);
}

/* 🔴 这条测试就是为了防住 [05] 里写反的那句 watermark 指导 */
TEST(test_fifo_a_full_is_free_space_not_sample_count)
{
	/* [ds] A_FULL 置位时 FIFO 内样本数 = 256 − FIFO_A_FULL */
	CHECK_EQ(max30131_fifo_a_full_from_batch(16), 240); /* 要 16 个 ⇒ 写 0xF0 */
	CHECK_EQ(max30131_fifo_batch_from_a_full(240), 16);

	/* 反面:照 [05] 字面写 16 会得到 240 个样本一批 */
	CHECK_EQ(max30131_fifo_batch_from_a_full(16), 240);

	/* 边界 */
	CHECK_EQ(max30131_fifo_a_full_from_batch(1), 255);
	CHECK_EQ(max30131_fifo_a_full_from_batch(256), 0);
	CHECK_EQ(max30131_fifo_batch_from_a_full(0), 256);
	CHECK_EQ(max30131_fifo_a_full_from_batch(0), 255); /* 0 视作 1 */
}

TEST(test_fifo_available)
{
	CHECK_EQ(max30131_fifo_available(0, 37), 37);
	CHECK_EQ(max30131_fifo_available(0, 0), 0);
	/* [ds] p71 伪码:OVF≠0 ⇒ 已丢数,按满 256 处理 */
	CHECK_EQ(max30131_fifo_available(1, 5), 256);
	CHECK_EQ(max30131_fifo_available(0x7F, 5), 256);
	/* COUNTER1 的 bit7 是 DATA_COUNT[8],不属于 OVF,不能误判成溢出 */
	CHECK_EQ(max30131_fifo_available(0x80, 12), 12);
}

/* ================================================================== */
/* 6. 时序表 + INVALID_CFG 约束                                        */
/* ================================================================== */
TEST(test_timing_tables)
{
	/* [05]/[ds] 本工作点:CONV_TIME=0x4,慢钟组,CLK_SEL=0 → 1.882s / 16 位 */
	CHECK_EQ(max30131_conv_time_ms(WP_CONV_TIME, WP_CLK_40K, false), 1882);
	CHECK_EQ(max30131_conv_time_bits(WP_CONV_TIME), 16);
	/* CLK_SEL=1 → 1.606s */
	CHECK_EQ(max30131_conv_time_ms(WP_CONV_TIME, true, false), 1606);
	/* 低码分辨率下降 */
	CHECK_EQ(max30131_conv_time_bits(0x0), 12);
	CHECK_EQ(max30131_conv_time_bits(0x3), 15);
	/* 快钟组同码更短 */
	CHECK_EQ(max30131_conv_time_ms(WP_CONV_TIME, WP_CLK_40K, true), 471);
	/* 0xB..0xF 夹在上限 */
	CHECK_EQ(max30131_conv_time_ms(0x0B, WP_CLK_40K, false), 240011);
	CHECK_EQ(max30131_conv_time_ms(0x0F, WP_CLK_40K, false), 240011);

	/* [05] SENS_PERIOD=0x5 → 3.757s */
	CHECK_EQ(max30131_sens_period_ms(WP_SENS_PERIOD, WP_CLK_40K), 3757);
	CHECK_EQ(max30131_sens_period_ms(0x4, WP_CLK_40K), 1882);
}

TEST(test_period_must_be_ge_conv_time)
{
	/* 本工作点合法:1.882s ≤ 3.757s */
	CHECK_EQ(max30131_check_period_vs_conv(WP_CONV_TIME, WP_SENS_PERIOD,
					       WP_CLK_40K, WP_FSR),
		 MAX30131_OK);
	/* 周期码降到 0x4(1.882s)刚好相等 → 仍合法 */
	CHECK_EQ(max30131_check_period_vs_conv(WP_CONV_TIME, 0x4, WP_CLK_40K,
					       WP_FSR),
		 MAX30131_OK);
	/* 周期码 0x3(0.945s)< 转换 1.882s → 会置 INVALID_CFG */
	CHECK_EQ(max30131_check_period_vs_conv(WP_CONV_TIME, 0x3, WP_CLK_40K,
					       WP_FSR),
		 MAX30131_ERR_CFG);
	/* 换到快钟组(FSR=1000nA)时同一对码变合法(转换只要 0.471s) */
	CHECK_EQ(max30131_check_period_vs_conv(WP_CONV_TIME, 0x3, WP_CLK_40K,
					       MAX30131_FSR_1000NA),
		 MAX30131_OK);
}

/* ================================================================== */
/* 7. 手动增益校准反算([ds] p41)                                      */
/* ================================================================== */
/*
 * ★ 差分换算必须让 ADC 固有偏移与 offset 源容差**双双消失** ★
 * 这是"标定后为什么能更准"的可测证明。
 */
TEST(test_diff_conversion_cancels_adc_offset_and_offset_tolerance)
{
	const int32_t fsr = 50000;   /* 假定已标定的真值 */
	const int32_t signal_pa = 1500; /* 待测还原电流 1.5nA */

	/* 造两组"器件很不理想"的情形:offset 源偏到边界 + ADC 有固有偏移 */
	struct {
		int32_t i_off_pa;  /* 真实 offset 源(容差内任意值) */
		int32_t adc_err_pa;/* ADC 固有偏移(±61pA 规格内) */
	} cases[] = {
		{ 9000, 0 },      /* typ,无偏移 */
		{ 7000, +61 },    /* offset 到 min + ADC 偏移拉满 */
		{ 11000, -61 },   /* offset 到 max + ADC 偏移反向拉满 */
		{ 8780, +30 },    /* 2026-07-31 实测值附近 */
	};

	for (size_t i = 0; i < ARRAY_SIZE_LOCAL(cases); i++) {
		int32_t off = cases[i].i_off_pa + cases[i].adc_err_pa;
		/* ADC 看到的两个 counts(还原电流使 counts 减小) */
		uint16_t c_off = max30131_reduction_pa_to_counts(0, fsr, off);
		uint16_t c_sig = max30131_reduction_pa_to_counts(signal_pa, fsr, off);

		int32_t got_fa = max30131_reduction_from_counts_diff_fa(c_off, c_sig,
								       fsr);
		/* 🔴 无论 offset 源与 ADC 偏移怎么变,差分结果都必须回到 1.5nA
		 * (残余只有 counts 量化的 ±1 LSB = ±763 fA) */
		CHECK_NEAR(got_fa, signal_pa * 1000, 800);
	}

	/*
	 * 反面对照:用**绝对 offset 版**且拿错标称值(10nA 而真值 8.78nA),
	 * 误差会高达 ~1.2nA —— 正是 2026-07-31 那个假象。
	 */
	int32_t c_real = max30131_reduction_pa_to_counts(signal_pa, fsr, 8780);
	int32_t wrong = max30131_counts_to_reduction_fa((uint16_t)c_real, fsr, 10000);

	CHECK_TRUE(wrong - signal_pa * 1000 > 1000000); /* 虚高 >1nA */
}

TEST(test_manual_gain_calibration)
{
	int32_t ioffset, fsr50;

	/*
	 * 场景:真实 offset 电流 = 10.2nA(标称 10nA,+2% 出厂偏差)。
	 * 步1 在 500nA 档(±1% 已校准)读到 counts = 10200/500000×65536 ≈ 1337
	 * 步2 在 50nA 档读到 counts;若该档增益真值偏大 2%(实际 FSR=51000pA),
	 *      则 counts = 10200/51000×65536 ≈ 13107
	 * 反算应得到 FSR₅₀ ≈ 51000pA 而不是标称 50000。
	 */
	ioffset = max30131_cal_ioffset_pa(1337, 500000);
	CHECK_NEAR(ioffset, 10200, 30);

	fsr50 = max30131_cal_fsr_pa(ioffset, 13107);
	CHECK_NEAR(fsr50, 51000, 200);

	/* 一致性:若两档都无偏差,反算应回到标称值 */
	ioffset = max30131_cal_ioffset_pa(1311, 500000); /* 10nA */
	fsr50 = max30131_cal_fsr_pa(ioffset, 13107);     /* 10nA on 50nA range */
	CHECK_NEAR(fsr50, 50000, 200);

	/* 除零保护 */
	CHECK_EQ(max30131_cal_fsr_pa(10000, 0), 0);
}

TEST(test_cv_eis_range_and_current_conversion)
{
	CHECK_EQ(max30131_eis_fsr_ua(MAX30131_EIS_FSR_4UA), 4);
	CHECK_EQ(max30131_eis_fsr_ua(MAX30131_EIS_FSR_8UA), 8);
	CHECK_EQ(max30131_eis_fsr_ua(MAX30131_EIS_FSR_20UA), 20);
	CHECK_EQ(max30131_eis_fsr_ua(MAX30131_EIS_FSR_40UA), 40);

	/* 50% offset centers zero at code 32768. CV/SWV has the documented 3/2 gain. */
	CHECK_EQ(max30131_cv_counts_to_iwe_fa(32768, MAX30131_EIS_FSR_20UA, 0), 0);
	CHECK_NEAR(max30131_cv_counts_to_iwe_fa(0, MAX30131_EIS_FSR_20UA, 0),
		   -15000000000LL, 1);
	CHECK_NEAR(max30131_cv_counts_to_iwe_fa(65535, MAX30131_EIS_FSR_20UA, 0),
		   14999542236LL, 1);
	/* Offset code 6 moves the zero-current code to 8192 (0.125 FSR). */
	CHECK_EQ(max30131_cv_counts_to_iwe_fa(8192, MAX30131_EIS_FSR_20UA, 6), 0);
}

/* ================================================================== */
int main(void)
{
	printf("=== MAX30131 纯逻辑层单测 ===\n");
	RUN(test_reg_hex_matches_design_doc);
	RUN(test_ce_dac_mx_default_would_give_zero_e);
	RUN(test_fsr_table);
	RUN(test_offset_table);
	RUN(test_offset_tolerance_bounds);
	RUN(test_ldopa_calibration_currents_fit_the_range);
	RUN(test_saturation_flags);
	RUN(test_offset_must_cover_signal_peak);
	RUN(test_counts_to_current);
	RUN(test_reduction_fa_keeps_sub_pa_resolution);
	RUN(test_ref_and_vdd);
	RUN(test_dac_code_and_byte_layout);
	RUN(test_polarization_sign);
	RUN(test_headroom);
	RUN(test_fifo_unpack);
	RUN(test_fifo_read_helper);
	RUN(test_fifo_a_full_is_free_space_not_sample_count);
	RUN(test_fifo_available);
	RUN(test_timing_tables);
	RUN(test_period_must_be_ge_conv_time);
	RUN(test_diff_conversion_cancels_adc_offset_and_offset_tolerance);
	RUN(test_manual_gain_calibration);
	RUN(test_cv_eis_range_and_current_conversion);
	return mt_report();
}
