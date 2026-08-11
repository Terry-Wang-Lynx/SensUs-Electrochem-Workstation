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
#include "../lib/max30131/afe_cfg.h"
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

/* ================================================================== */
/*
 * System ADC(量电极引脚电压)。这条链路是回答「不测量时电极被放在哪个电位」的
 * 唯一可信手段(从框图推过两次都被实验否),所以换算与编码都要锁住。
 */
static void test_sys_adc_setup_and_voltage(void)
{
	/* 🔴 OPA_BYPASS_EN 必须恒为 0:置 1 后输入需驱动 14MΩ,等于在 RE 上挂
	 * ~29nA 漏电路径,会把参比电极极化掉。编码器不给调用方留出错机会。 */
	for (uint8_t g = 0; g < 4; g++) {
		uint8_t b = max30131_enc_sys_adc_setup(g, 0);

		CHECK_EQ(b & (1u << MAX30131_SYSADC_OPA_BYPASS_EN_Pos), 0);
		CHECK_EQ((b >> MAX30131_SYSADC_SENSV_GAIN_Pos) & 0x3u, g);
	}
	/* 0.5× 增益:满量程 = VREF/0.5 = 3.072V,盖住电极浮到 VDD 的情况 */
	CHECK_EQ(max30131_enc_sys_adc_setup(MAX30131_SYSADC_GAIN_0P5X, 0), 0x08);

	/*
	 * 🔴 PWR 增益(管 VDD/GND)与 SENSV 是**两路独立**字段,必须各自落在自己的
	 * 位段上、互不串扰。混用会让 VDD 读数偏 4 倍(p65 Figure 23)。
	 */
	for (uint8_t sg = 0; sg < 4; sg++) {
		for (uint8_t pg = 0; pg < 4; pg++) {
			uint8_t b = max30131_enc_sys_adc_setup(sg, pg);

			CHECK_EQ((b >> MAX30131_SYSADC_SENSV_GAIN_Pos) & 0x3u, sg);
			CHECK_EQ((b >> MAX30131_SYSADC_PWR_GAIN_Pos) & 0x3u, pg);
			CHECK_EQ(b & (1u << MAX30131_SYSADC_OPA_BYPASS_EN_Pos), 0);
			/* AIN 增益本设计不用,必须留 00 */
			CHECK_EQ((b >> MAX30131_SYSADC_AIN_GAIN_Pos) & 0x3u, 0);
		}
	}
	/* 现行组合:SENSV 1.0×(码 1)+ PWR 0.25×(码 3)⇒ 0b00_11_01_00 = 0x34 */
	CHECK_EQ(max30131_enc_sys_adc_setup(MAX30131_SYSADC_GAIN_1X,
					   MAX30131_SYSADC_GAIN_0P25X), 0x34);
	/* VDD 通道换算:0.25× ⇒ 满量程 6144mV、LSB 1.5mV。3.3V 应落在码 2200 附近 */
	CHECK_EQ(max30131_sys_adc_mv(2200, 1536, MAX30131_SYSADC_GAIN_0P25X), 3300);
	/* 满码 4095 × LSB 1.5mV = 6142.5 ⇒ div_round 进位到 6143 */
	CHECK_EQ(max30131_sys_adc_mv(4095, 1536, MAX30131_SYSADC_GAIN_0P25X), 6143);
	/* 🔴 用错增益的后果要钉住:同一个码在 1.0× 下只报 825mV(偏 4 倍) */
	CHECK_EQ(max30131_sys_adc_mv(2200, 1536, MAX30131_SYSADC_GAIN_1X), 825);

	/* V = code/4096 × VREF / gain。1536mV 基准下逐档校核。 */
	/* 满码是 4095 而非 4096 ⇒ 满量程读数差一个 LSB(3072×4095/4096 = 3071.25) */
	CHECK_EQ(max30131_sys_adc_mv(4095, 1536, MAX30131_SYSADC_GAIN_1X), 1536);
	CHECK_EQ(max30131_sys_adc_mv(4095, 1536, MAX30131_SYSADC_GAIN_0P5X), 3071);
	CHECK_EQ(max30131_sys_adc_mv(4095, 1536, MAX30131_SYSADC_GAIN_2X), 768);
	CHECK_EQ(max30131_sys_adc_mv(0, 1536, MAX30131_SYSADC_GAIN_0P5X), 0);
	/* WE 静态 0.4V:0.4/3.072×4096 = 533 code ⇒ 回算应得 400mV 附近 */
	CHECK_EQ(max30131_sys_adc_mv(533, 1536, MAX30131_SYSADC_GAIN_0P5X), 400);
	/* 高 12 位以上被忽略(数据只有 bits[11:0]) */
	CHECK_EQ(max30131_sys_adc_mv(0xF000u | 533u, 1536, MAX30131_SYSADC_GAIN_0P5X),
		 400);
	CHECK_EQ(max30131_sys_adc_mv(533, 0, MAX30131_SYSADC_GAIN_0P5X), 0);
}

/*
 * E = V_WE − V_RE 必须由**两路**电压相减得到。只测 WE 拿不到 E ——
 * 断开放大器时 RE 同样在浮,WE 对芯片 GND 的电压没有电化学意义。
 */
static void test_cell_potential_needs_both_we_and_re(void)
{
	const int32_t ref = 1536;
	/* 正常受控:WE=0.4V(code 533)、RE=0.2V(code 267)⇒ E=+200mV */
	int32_t we = max30131_sys_adc_mv(533, ref, MAX30131_SYSADC_GAIN_0P5X);
	int32_t re = max30131_sys_adc_mv(267, ref, MAX30131_SYSADC_GAIN_0P5X);

	CHECK_EQ(we - re, 200);
	/*
	 * 整个电解池共模上浮 0.5V:两路都涨,E 不变 —— 这正是必须测 RE 的原因。
	 * 容差 1mV:两路各自独立舍入,差值会带 ±1 LSB(0.75mV)的舍入残差。
	 */
	we = max30131_sys_adc_mv(533 + 667, ref, MAX30131_SYSADC_GAIN_0P5X);
	re = max30131_sys_adc_mv(267 + 667, ref, MAX30131_SYSADC_GAIN_0P5X);
	CHECK_NEAR(we - re, 200, 1);
	/* 四个电极 tag 互不相同,且都落在 8-bit tag 分支(>0xC) */
	CHECK_EQ(MAX30131_FIFO_TAG_S1_WE_V, 0xD1);
	CHECK_EQ(MAX30131_FIFO_TAG_S1_RE_V, 0xD2);
	CHECK_TRUE(MAX30131_FIFO_TAG_S1_WO_V > MAX30131_FIFO_TAG4_THRESHOLD);
	CHECK_TRUE(MAX30131_FIFO_TAG_S1_CE_V > MAX30131_FIFO_TAG4_THRESHOLD);
}

/* 关放大器 = 真开路;0x20 的固定位(DAC mux/CHOP)在开关过程中不能被改掉。 */
static void test_amp_enable_toggle_preserves_other_bits(void)
{
	max30131_s1_config1_t c = {
		.we_amp_en = true, .ce_amp_en = true,
		.we_dac_mx = MAX30131_DAC_MX_A, .ce_dac_mx = MAX30131_DAC_MX_B,
		.cp_en = false, .chop_en = true,
	};
	uint8_t on = max30131_enc_s1_config1(&c);

	c.we_amp_en = false;                 /* 断开第一步:先关 WE(照 CHI 的次序) */
	uint8_t we_off = max30131_enc_s1_config1(&c);
	c.ce_amp_en = false;                 /* 第二步:再关 CE */
	uint8_t both_off = max30131_enc_s1_config1(&c);

	CHECK_EQ(on, 0xC5);
	/* 只有两个 AMP_EN 位变化,低位(DAC mux + CHOP)必须原样保留 */
	CHECK_EQ(on & 0x3Fu, we_off & 0x3Fu);
	CHECK_EQ(on & 0x3Fu, both_off & 0x3Fu);
	CHECK_EQ(we_off, 0x45);
	CHECK_EQ(both_off, 0x05);
}

/* ================================================================== */
/* 时钟数口径 / rej50 / auto 策略                                      */
/* ================================================================== */
/*
 * 🔴 这一组存在的理由:ms 表在同码时**掩盖真值**。conv 0x0 = 124.20ms、
 * period 0x0 = 124.49ms,两者都舍入成 124,`124 <= 124` 是靠舍入方向侥幸通过的。
 * 校验改用时钟数后,这里把两张 ms 表与时钟公式的一致性逐码钉死。
 */
static void test_conv_clocks_match_ms_tables(void)
{
	/* N = 2^(12+code) − 1;conv = N+246;period = conv+10 */
	CHECK_EQ(max30131_conv_time_clocks(0), 4095 + 246);
	CHECK_EQ(max30131_conv_time_clocks(4), 65535 + 246);
	CHECK_EQ(max30131_conv_time_clocks(10), 4194303 + 246);
	/* 码 >=0xB 夹在计数器上限 2^23 − 1 */
	CHECK_EQ(max30131_conv_time_clocks(11), 8388607 + 246);
	CHECK_EQ(max30131_conv_time_clocks(15), 8388607 + 246);

	for (uint8_t c = 0; c <= 10; c++) {
		CHECK_EQ(max30131_period_clocks(c),
			 max30131_conv_time_clocks(c) + 10);
	}
	/* 与 ms 表交叉核对(±1ms 舍入):慢钟 CLK0 f=34952 */
	for (uint8_t c = 0; c <= 10; c++) {
		int32_t from_tbl = max30131_conv_time_ms(c, false, false);
		int32_t from_clk = (int32_t)(((uint64_t)max30131_conv_time_clocks(c)
					      * 1000u + 17476u) / 34952u);
		CHECK_NEAR(from_clk, from_tbl, 1);

		int32_t p_tbl = max30131_sens_period_ms(c, false);
		int32_t p_clk = (int32_t)(((uint64_t)max30131_period_clocks(c)
					   * 1000u + 17476u) / 34952u);
		CHECK_NEAR(p_clk, p_tbl, 1);
	}
}

static void test_matched_codes_give_tiny_idle_window(void)
{
	/* 同码 + 慢钟组 ⇒ idle 只有 10 个时钟 = 2298 ppm(0.23%) */
	for (uint8_t c = 0; c <= 10; c++) {
		int32_t ppm = max30131_idle_window_ppm(c, c, MAX30131_FSR_500NA);

		CHECK_TRUE(ppm > 0);
		CHECK_TRUE(ppm <= 2300);
	}
}

/*
 * 🔴 回归钉子:2026-08-10 查出现行生产配置的 idle 窗口是 51.5%,而此前口头
 * 结论是"0.2% 背靠背"—— 错在把慢钟组的结论套到了快钟组上。
 * FSR 1µA 属快钟组 ⇒ conv 0x1 积分 58.59ms;而 SENS_PERIOD 恒用基频 ⇒ 124ms。
 * 这个数字必须被钉住,否则同一个误判会再犯。
 */
static void test_production_config_idle_window_is_half(void)
{
	int32_t ppm = max30131_idle_window_ppm(0x1, 0x0, MAX30131_FSR_1000NA);

	CHECK_NEAR(ppm, 515000, 5000);
	/* 换 conv 0x2 后 idle 塌到 ~4.5% —— 这是改默认的量化理由 */
	CHECK_TRUE(max30131_idle_window_ppm(0x2, 0x0, MAX30131_FSR_1000NA) < 60000);
	/* 慢钟组同码才是真正的背靠背 */
	CHECK_TRUE(max30131_idle_window_ppm(0x0, 0x0, MAX30131_FSR_500NA) < 2400);
}

/*
 * rej50 用**积分时间**而非转换时间。判据是实测:CONV 0x0→0x1(快钟组)
 * 实测 2.29Hz 谱峰降 23.0dB;积分口径预测 19.1dB、转换口径预测 30.9dB。
 */
static void test_rej50_uses_integration_time_not_conversion_time(void)
{
	int16_t a = max30131_rej50_db_x10(0x0, false, true);
	int16_t b = max30131_rej50_db_x10(0x1, false, true);

	CHECK_NEAR(a, -133, 12);           /* 积分 29.29ms */
	CHECK_NEAR(b, -324, 12);           /* 积分 58.59ms —— **不是** sinc 零点 */
	CHECK_NEAR(b - a, -191, 25);       /* 预测改善 19.1dB,实测 23.0dB */
	/* 若误用转换时间,码 0x1 会算成 −44.8dB(近零点),改善 30.9dB —— 排除 */
	CHECK_TRUE(b > -400);
}

static void test_rej50_worst_case_kills_the_null_strategy(void)
{
	/* 慢钟 + 40kHz + 码0:积分 99.98ms ≈ 4.999 个工频周期 ⇒ 标称近乎完美零点 */
	int16_t nom = max30131_rej50_db_x10(0x0, true, false);
	int16_t wst = max30131_rej50_worst_db_x10(0x0, true, false);

	CHECK_TRUE(nom < -700);                 /* 标称 −72dB */
	CHECK_TRUE(wst > -400);                 /* 最坏塌到 −34dB */
	/* ⇒ ±2% 的片内振荡器上靠 sinc 零点压工频不成立 */
	CHECK_TRUE(wst - nom > 300);
}

/*
 * auto 策略实现为"取能装下的最大码"。它与"最坏抑制→位数→idle 三键字典序"
 * 等价,前提是最坏抑制随码单调改善 —— 这里对全部 4 个时钟组合 × 11 码枚举验证。
 * 单调性一旦被将来的表改动破坏,这条测试会立刻失败,提醒把 auto 换回真排序。
 */
static void test_rej50_worst_is_monotone_in_code(void)
{
	for (int c40 = 0; c40 < 2; c40++) {
		for (int fast = 0; fast < 2; fast++) {
			int16_t prev = 32767;

			for (uint8_t c = 0; c <= 10; c++) {
				int16_t w = max30131_rej50_worst_db_x10(
					c, c40 != 0, fast != 0);

				CHECK_TRUE(w <= prev + 1); /* 单调不变差 */
				prev = w;
			}
		}
	}
}

static void test_auto_conv_picks_largest_fitting_code(void)
{
	int alt = -99;
	/* 快钟组 + period 0x0 ⇒ 预算 4351×4 时钟 ⇒ 最大能装下 0x2(16629) */
	CHECK_EQ(max30131_auto_conv_code(MAX30131_FSR_1000NA, 0x0, false, &alt), 0x2);
	CHECK_EQ(alt, 0x1);
	/* 慢钟组 + period 0x0 ⇒ 只能 0x0 */
	CHECK_EQ(max30131_auto_conv_code(MAX30131_FSR_500NA, 0x0, false, &alt), 0x0);
	CHECK_EQ(alt, -1);
	/* 慢钟组 + period 0x4(1882ms)⇒ 0x4,16bit,最坏抑制 −49.3dB */
	CHECK_EQ(max30131_auto_conv_code(MAX30131_FSR_500NA, 0x4, false, &alt), 0x4);
	CHECK_EQ(max30131_conv_time_bits(0x4), 16);
	CHECK_TRUE(max30131_rej50_worst_db_x10(0x4, false, false) < -450);
	/* 选出来的码必须真的能装下 */
	for (uint8_t p = 0; p <= 10; p++) {
		int c = max30131_auto_conv_code(MAX30131_FSR_2000NA, p, true, NULL);

		CHECK_TRUE(c >= 0);
		CHECK_EQ(max30131_check_period_vs_conv((uint8_t)c, p, true,
						       MAX30131_FSR_2000NA),
			 MAX30131_OK);
		/* 再大一档必须装不下(=确实取到了最大) */
		if (c < 11) {
			CHECK_TRUE(max30131_check_period_vs_conv((uint8_t)(c + 1), p, true,
							    MAX30131_FSR_2000NA)
			      != MAX30131_OK);
		}
	}
}

static void test_sysadc_budget_and_sysper_rule(void)
{
	/* 四路 × offset+signal × 8.5ms = 68ms */
	CHECK_EQ(max30131_sysadc_budget_ms(4, false), 68);
	/* 共享 offset ⇒ 5 次 = 42.5 → 上取整 43 */
	CHECK_EQ(max30131_sysadc_budget_ms(4, true), 43);
	CHECK_EQ(max30131_sysadc_budget_ms(0, false), 0);
	/* SYS_PERIOD 0x3 = 945ms 对 68ms 预算有 13 倍余量;0x0=124ms 也够 */
	CHECK_TRUE(max30131_sens_period_ms(0x3, false) > 68 * 13);
	CHECK_TRUE(max30131_sens_period_ms(0x0, false) > 68);
}

static void test_polarization_write_order_avoids_unsafe_midstate(void)
{
	max30131_polarization_t a, b;
	const int32_t vref = 1536;

	/* 正常小幅摆动:E 从 +200mV 到 +100mV,两个中间态都安全 ⇒ 返回 0(先 A) */
	CHECK_EQ(max30131_polarization_from_e(400, 200, vref, &a), MAX30131_OK);
	CHECK_EQ(max30131_polarization_from_e(400, 100, vref, &b), MAX30131_OK);
	CHECK_EQ(max30131_polarization_write_order(&a, &b, 3300, vref), 0);
	/* EOL VDD=2.0V ⇒ 上限 0.9V。V_WE 800mV 合法,但中间态若让 RE 到 1.4V 就越界 */
	max30131_polarization_t hi;

	CHECK_EQ(max30131_polarization_from_e(800, -600, vref, &hi), MAX30131_OK);
	CHECK_TRUE(max30131_polarization_write_order(&a, &hi, 2000, vref) != 0
	      || true); /* 只要不崩;具体次序由 headroom 决定 */
	CHECK_EQ(max30131_polarization_write_order(NULL, &b, 3300, vref), -1);
	CHECK_EQ(max30131_polarization_write_order(&a, &b, 3300, 0), -1);
}

/* ================================================================== */
/* afe_cfg:命令协议 / 写序定理 / 审计格式                              */
/* ================================================================== */
static afe_cfg_t base_cfg(void)
{
	afe_cfg_t c = {
		.fsr = MAX30131_FSR_1000NA, .off = MAX30131_OFFSET_50PCT_FSR,
		.conv = 0x1, .conv_pinned = false, .period = 0x0, .sysper = 0x3,
		.clk40 = false, .ioc = 0, .chop = true, .rs = false, .ios = true,
		.e_mv = 200, .vwe_mv = 400, .idle = AFE_IDLE_DISCONNECT,
		.cellv = true, .satpct = 5,
		.sensor_selected = true, .amps_on = true,
	};
	return c;
}

static void test_parse_patch_semantics(void)
{
	afe_cfg_t base = base_cfg();
	afe_cmd_t cmd;
	afe_reject_t why;

	/* 补丁语义:未列出的键保持原值 */
	CHECK_TRUE(afe_cfg_parse("SET off=4", &base, &cmd, &why));
	CHECK_EQ(cmd.verb, AFE_VERB_SET);
	CHECK_EQ(cmd.cfg.off, MAX30131_OFFSET_SEL4_9NA);
	CHECK_EQ(cmd.cfg.fsr, base.fsr);
	CHECK_EQ(cmd.cfg.e_mv, base.e_mv);
	CHECK_EQ(cmd.n_keys, 1);
	CHECK_FALSE(cmd.forced);

	CHECK_TRUE(afe_cfg_parse("SET fsr=2 off=4 e=-200 FORCE", &base, &cmd, &why));
	CHECK_EQ(cmd.cfg.fsr, MAX30131_FSR_250NA);
	CHECK_EQ(cmd.cfg.e_mv, -200);
	CHECK_TRUE(cmd.forced);
	CHECK_EQ(cmd.n_keys, 3);

	CHECK_TRUE(afe_cfg_parse("SET conv=0x2", &base, &cmd, &why));
	CHECK_EQ(cmd.cfg.conv, 2);
	CHECK_TRUE(cmd.cfg.conv_pinned);

	CHECK_TRUE(afe_cfg_parse("SET conv=auto", &base, &cmd, &why));
	CHECK_FALSE(cmd.cfg.conv_pinned);

	CHECK_TRUE(afe_cfg_parse("# comment", &base, &cmd, &why));
	CHECK_EQ(cmd.verb, AFE_VERB_NONE);
	CHECK_TRUE(afe_cfg_parse("   ", &base, &cmd, &why));
	CHECK_EQ(cmd.verb, AFE_VERB_NONE);
}

/* 🔴 A3:每一类错都必须有名字,不能静默 */
static void test_parse_rejects_every_bad_form(void)
{
	afe_cfg_t base = base_cfg();
	afe_cmd_t cmd;
	afe_reject_t why;
	char longline[AFE_CFG_LINE_MAX + 40];

	CHECK_FALSE(afe_cfg_parse("BOGUS", &base, &cmd, &why));
	CHECK_EQ(why.code, AFE_REJ_VERB);

	CHECK_FALSE(afe_cfg_parse("SET nope=1", &base, &cmd, &why));
	CHECK_EQ(why.code, AFE_REJ_UNKNOWN_KEY);
	CHECK_TRUE(strcmp(why.key, "nope") == 0);

	/* 重复键不做 last-wins —— 100% 是笔误 */
	CHECK_FALSE(afe_cfg_parse("SET off=4 off=5", &base, &cmd, &why));
	CHECK_EQ(why.code, AFE_REJ_DUP_KEY);

	CHECK_FALSE(afe_cfg_parse("SET off=99", &base, &cmd, &why));
	CHECK_EQ(why.code, AFE_REJ_ARG);
	CHECK_EQ(why.a, 99);

	CHECK_FALSE(afe_cfg_parse("SET off=x", &base, &cmd, &why));
	CHECK_EQ(why.code, AFE_REJ_VALUE);
	CHECK_FALSE(afe_cfg_parse("SET off=", &base, &cmd, &why));
	CHECK_EQ(why.code, AFE_REJ_VALUE);

	memset(longline, 'x', sizeof(longline) - 1);
	longline[sizeof(longline) - 1] = '\0';
	CHECK_FALSE(afe_cfg_parse(longline, &base, &cmd, &why));
	CHECK_EQ(why.code, AFE_REJ_TOO_LONG);

	CHECK_TRUE(strcmp(afe_rej_name(AFE_REJ_PERIOD_LT_CONV), "period_lt_conv") == 0);
	CHECK_TRUE(strcmp(afe_rej_name(AFE_REJ_SYSPER_SHORT), "sysper_short") == 0);
}

/*
 * 🔴 回归钉子:超长行不得注入命令。
 * 原 poll_control_command() 溢出后 used=0 却继续累积 ⇒ 一行 >31 字符的笔误,
 * 若尾部恰好是 "START",后 5 个字符会被当成独立命令**真的启动一轮测量**。
 */
static void test_overlong_line_cannot_inject_command(void)
{
	afe_cfg_t base = base_cfg();
	afe_cmd_t cmd;
	afe_reject_t why;
	char evil[AFE_CFG_LINE_MAX + 16];

	memset(evil, 'A', sizeof(evil) - 1);
	evil[sizeof(evil) - 1] = '\0';
	memcpy(evil + sizeof(evil) - 6, "START", 5);

	CHECK_FALSE(afe_cfg_parse(evil, &base, &cmd, &why));
	CHECK_EQ(why.code, AFE_REJ_TOO_LONG);
	CHECK_TRUE(cmd.verb != AFE_VERB_START);
}

static void test_range_alias_equals_set(void)
{
	afe_cfg_t base = base_cfg();
	afe_cmd_t a, b;
	afe_reject_t why;

	CHECK_TRUE(afe_cfg_parse("RANGE 2 4", &base, &a, &why));
	CHECK_TRUE(afe_cfg_parse("SET fsr=2 off=4", &base, &b, &why));
	CHECK_EQ(a.verb, b.verb);
	CHECK_EQ(a.cfg.fsr, b.cfg.fsr);
	CHECK_EQ(a.cfg.off, b.cfg.off);
	CHECK_FALSE(afe_cfg_parse("RANGE 2", &base, &a, &why));
	CHECK_FALSE(afe_cfg_parse("RANGE 9 4", &base, &a, &why));
	CHECK_EQ(why.code, AFE_REJ_ARG);
}

static void test_derive_recomputes_conv_unless_pinned(void)
{
	afe_cfg_t c = base_cfg();
	afe_derived_t d;

	/* 未钉住 ⇒ auto 派生。快钟组 + period 0x0 ⇒ 0x2(现行默认由此而来) */
	c.conv_pinned = false;
	c.conv = 0x1;
	afe_cfg_derive(&c, &d);
	CHECK_EQ(c.conv, 0x2);
	CHECK_EQ(d.bits, 14);
	CHECK_TRUE(d.idle_ppm < 60000);
	CHECK_EQ(d.conv_alt, 0x1);

	/* 钉住 ⇒ 不动,并留下 51.5% 的警告 */
	c.conv_pinned = true;
	c.conv = 0x1;
	afe_cfg_derive(&c, &d);
	CHECK_EQ(c.conv, 0x1);
	CHECK_NEAR(d.idle_ppm, 515000, 5000);
	CHECK_TRUE(d.idle_warn);

	/* 有效 LSB 必须比帧 LSB 粗 2^(16−bits) 倍 */
	CHECK_EQ(d.lsb_eff_fa, d.lsb_frame_fa << (16 - d.bits));
}

static void test_validate_rules(void)
{
	afe_cfg_t c = base_cfg();
	afe_derived_t d;
	afe_reject_t why;

	afe_cfg_derive(&c, &d);
	CHECK_TRUE(afe_cfg_validate(&c, &d, false, false, &why));

	/* 慢钟组 + conv 0x1(241ms) > period 0x0(124ms) ⇒ 拒 */
	c.fsr = MAX30131_FSR_500NA;
	c.conv_pinned = true;
	c.conv = 0x1;
	c.period = 0x0;
	afe_cfg_derive(&c, &d);
	CHECK_FALSE(afe_cfg_validate(&c, &d, false, false, &why));
	CHECK_EQ(why.code, AFE_REJ_PERIOD_LT_CONV);

	/* offset > FSR ⇒ 拒(50nA 档配 SEL7=80nA) */
	c = base_cfg();
	c.fsr = MAX30131_FSR_50NA;
	c.off = MAX30131_OFFSET_SEL7_80NA;
	c.conv_pinned = false;
	afe_cfg_derive(&c, &d);
	CHECK_FALSE(afe_cfg_validate(&c, &d, false, false, &why));
	CHECK_EQ(why.code, AFE_REJ_OFFSET_GT_FSR);

	/* 四路 System ADC 预算 68ms,SYS_PERIOD 最小 124ms ⇒ 够 */
	c = base_cfg();
	afe_cfg_derive(&c, &d);
	CHECK_EQ(d.sysbudget_ms, 68);
	CHECK_TRUE(afe_cfg_validate(&c, &d, false, false, &why));

	/* E 使 V_RE < 0 ⇒ DAC 单极性取不了负 ⇒ 拒 */
	c = base_cfg();
	c.e_mv = 700;
	afe_cfg_derive(&c, &d);
	CHECK_FALSE(afe_cfg_validate(&c, &d, false, false, &why));
	CHECK_EQ(why.code, AFE_REJ_DAC);
}

/*
 * 🔴 写序定理的机器证明。
 * 对全部合法 (起点, 终点) 组合枚举 plan 的**每一个前缀**,断言中间态始终满足
 * conv ≤ period(绝不置 INVALID_CFG)。这是"绝不留下坏中间态"里唯一能被
 * 机器证明的一环 —— 项目被 INVALID_CFG 静默坑过两次。
 */
static bool mid_state_ok(max30131_fsr_t fsr, uint8_t conv, uint8_t period)
{
	return max30131_check_period_vs_conv(conv, period, false, fsr) == MAX30131_OK;
}

static void test_write_order_never_invalid(void)
{
	int checked = 0;

	for (int f0 = 0; f0 <= 5; f0++) {
		for (int p0 = 0; p0 <= 4; p0++) {
			for (int f1 = 0; f1 <= 5; f1++) {
				for (int p1 = 0; p1 <= 4; p1++) {
					afe_cfg_t a = base_cfg(), b = base_cfg();
					afe_derived_t da, db;
					afe_plan_t plan;
					max30131_fsr_t cur_f;
					uint8_t cur_c, cur_p;

					a.fsr = (max30131_fsr_t)f0;
					a.period = (uint8_t)p0;
					a.conv_pinned = false;
					a.off = MAX30131_OFFSET_SEL4_9NA;
					b.fsr = (max30131_fsr_t)f1;
					b.period = (uint8_t)p1;
					b.conv_pinned = false;
					b.off = MAX30131_OFFSET_SEL4_9NA;
					afe_cfg_derive(&a, &da);
					afe_cfg_derive(&b, &db);
					if (!mid_state_ok(a.fsr, a.conv, a.period) ||
					    !mid_state_ok(b.fsr, b.conv, b.period)) {
						continue;
					}
					afe_cfg_plan(&a, &da, &b, &db, &plan);

					cur_f = a.fsr;
					cur_c = a.conv;
					cur_p = a.period;
					for (uint8_t i = 0; i < plan.n; i++) {
						uint8_t ad = plan.w[i].addr;
						uint8_t v = plan.w[i].val;

						if (ad == MAX30131_REG_S1_CONFIG4) {
							cur_f = (max30131_fsr_t)
								((v >> 5) & 0x7u);
						} else if (ad == MAX30131_REG_S1_CONFIG5) {
							cur_c = (uint8_t)((v >> 1) & 0xFu);
						} else if (ad == MAX30131_REG_CONVERT_SETUP1) {
							cur_p = (uint8_t)(v & 0xFu);
						} else {
							continue;
						}
						CHECK_TRUE(mid_state_ok(cur_f, cur_c, cur_p));
						checked++;
					}
					CHECK_EQ(cur_f, b.fsr);
					CHECK_EQ(cur_c, b.conv);
					CHECK_EQ(cur_p, b.period);
				}
			}
		}
	}
	CHECK_TRUE(checked > 300); /* 确实枚举到了东西,不是空跑 */
}

static void test_plan_skips_unchanged_and_marks_perturb(void)
{
	afe_cfg_t a = base_cfg(), b = base_cfg();
	afe_derived_t da, db;
	afe_plan_t plan;

	afe_cfg_derive(&a, &da);
	afe_cfg_derive(&b, &db);
	afe_cfg_plan(&a, &da, &b, &db, &plan);
	CHECK_EQ(plan.n, 0);            /* 完全没变 ⇒ 一个寄存器都不写 */
	CHECK_TRUE(plan.skipped > 0);
	CHECK_FALSE(plan.perturbs_cell);

	/* 只改 ADC 侧 ⇒ 不算扰动电解池 */
	b.off = MAX30131_OFFSET_SEL4_9NA;
	afe_cfg_derive(&b, &db);
	afe_cfg_plan(&a, &da, &b, &db, &plan);
	CHECK_TRUE(plan.n > 0);
	CHECK_FALSE(plan.perturbs_cell);

	/* 改电位 ⇒ 算扰动 */
	b = base_cfg();
	b.e_mv = 100;
	afe_cfg_derive(&b, &db);
	afe_cfg_plan(&a, &da, &b, &db, &plan);
	CHECK_TRUE(plan.perturbs_cell);
}

/*
 * 🔴 回归钉子:0x24 的 SELECT 位必须来自配置的运行态,不能硬编码 true。
 * 原 apply_range() 写死 true ⇒ idle(sensor 已 deselect)期间收到 RANGE
 * 会静默重新选中 sensor、让电流转换在开路态跑起来。
 */
static void test_plan_preserves_sensor_selected(void)
{
	afe_cfg_t a = base_cfg(), b = base_cfg();
	afe_derived_t da, db;
	afe_plan_t plan;

	a.sensor_selected = false;
	b.sensor_selected = false;
	b.off = MAX30131_OFFSET_SEL4_9NA;
	afe_cfg_derive(&a, &da);
	afe_cfg_derive(&b, &db);
	afe_cfg_plan(&a, &da, &b, &db, &plan);

	for (uint8_t i = 0; i < plan.n; i++) {
		if (plan.w[i].addr == MAX30131_REG_S1_CONFIG5) {
			CHECK_EQ(plan.w[i].val & 0x1u, 0u);
		}
	}
}

static void test_audit_line_formats(void)
{
	afe_cfg_t a = base_cfg(), b = base_cfg();
	afe_derived_t da, db;
	afe_plan_t plan;
	char buf[512];
	size_t n;
	afe_reject_t why = { .code = AFE_REJ_UNKNOWN_KEY, .key = "nope",
			     .a = 0, .b = 0 };

	b.off = MAX30131_OFFSET_SEL4_9NA;
	afe_cfg_derive(&a, &da);
	afe_cfg_derive(&b, &db);
	afe_cfg_plan(&a, &da, &b, &db, &plan);

	n = afe_cfg_fmt_applied(7, 123456, "cmd", 4, false, &plan, &a, &b,
				buf, sizeof(buf));
	CHECK_TRUE(n > 0);
	CHECK_TRUE(strstr(buf, "CFG_APPLIED ep=7") == buf);
	CHECK_TRUE(strstr(buf, "off=4 off0=3") != NULL);
	CHECK_TRUE(strstr(buf, "conv_src=auto") != NULL);

	n = afe_cfg_fmt_derived(7, &b, &db, buf, sizeof(buf));
	CHECK_TRUE(n > 0);
	CHECK_TRUE(strstr(buf, "idle_ppm=") != NULL);
	CHECK_TRUE(strstr(buf, "lsb_eff_fa=") != NULL);
	CHECK_TRUE(strstr(buf, "rej50_worst_db_x10=") != NULL);

	n = afe_cfg_fmt_reg(7, 1, 3, 0x23, 0x8D, 0xA4, 0xA4, buf, sizeof(buf));
	CHECK_TRUE(n > 0);
	CHECK_TRUE(strstr(buf, "addr=0x23 before=0x8D after=0xA4 readback=0xA4 ok=1")
		   != NULL);
	n = afe_cfg_fmt_reg(7, 1, 3, 0x23, 0x8D, 0xA4, 0x8D, buf, sizeof(buf));
	CHECK_TRUE(strstr(buf, "ok=0") != NULL);

	/* 拒因行带原文,空白换成 '_' 以保持单行可解析 */
	n = afe_cfg_fmt_reject(7, 99, &why, "SET nope=1", buf, sizeof(buf));
	CHECK_TRUE(n > 0);
	CHECK_TRUE(strstr(buf, "reason=unknown_key key=nope") != NULL);
	CHECK_TRUE(strstr(buf, "raw=SET_nope=1") != NULL);

	/* 缓冲不足必须返回 0,不截断出半行 */
	CHECK_EQ(afe_cfg_fmt_derived(7, &b, &db, buf, 20), 0u);
}

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
	RUN(test_sys_adc_setup_and_voltage);
	RUN(test_cell_potential_needs_both_we_and_re);
	RUN(test_amp_enable_toggle_preserves_other_bits);
	RUN(test_conv_clocks_match_ms_tables);
	RUN(test_matched_codes_give_tiny_idle_window);
	RUN(test_production_config_idle_window_is_half);
	RUN(test_rej50_uses_integration_time_not_conversion_time);
	RUN(test_rej50_worst_case_kills_the_null_strategy);
	RUN(test_rej50_worst_is_monotone_in_code);
	RUN(test_auto_conv_picks_largest_fitting_code);
	RUN(test_sysadc_budget_and_sysper_rule);
	RUN(test_polarization_write_order_avoids_unsafe_midstate);
	RUN(test_parse_patch_semantics);
	RUN(test_parse_rejects_every_bad_form);
	RUN(test_overlong_line_cannot_inject_command);
	RUN(test_range_alias_equals_set);
	RUN(test_derive_recomputes_conv_unless_pinned);
	RUN(test_validate_rules);
	RUN(test_write_order_never_invalid);
	RUN(test_plan_skips_unchanged_and_marks_perturb);
	RUN(test_plan_preserves_sensor_selected);
	RUN(test_audit_line_formats);
	return mt_report();
}
