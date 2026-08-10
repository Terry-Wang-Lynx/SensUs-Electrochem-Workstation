/*
 * max30131.c — MAX30131 纯逻辑层实现(零硬件依赖,可在开发机直接编译单测)
 *
 * 用途/用法/前置条件/快照日期见 max30131.h。
 * 纪律:本文件内不得出现任何 Zephyr / nrfx / 平台头文件。
 */

#include "max30131.h"

/* ------------------------------------------------------------------ */
/* 小工具                                                              */
/* ------------------------------------------------------------------ */
#define BIT_IF(cond, pos) ((cond) ? (uint8_t)(1u << (pos)) : 0u)

/* 带四舍五入的除法(分母为正) */
static int64_t div_round(int64_t num, int64_t den)
{
	if (num >= 0) {
		return (num + den / 2) / den;
	}
	return -((-num + den / 2) / den);
}

/* ------------------------------------------------------------------ */
/* FSR                                                                 */
/* ------------------------------------------------------------------ */
int32_t max30131_fsr_pa(max30131_fsr_t fsr)
{
	switch (fsr) {
	case MAX30131_FSR_50NA:
		return 50000;
	case MAX30131_FSR_100NA:
		return 100000;
	case MAX30131_FSR_250NA:
		return 250000; /* 勘误点:不是 200000 */
	case MAX30131_FSR_500NA:
		return 500000;
	case MAX30131_FSR_1000NA:
		return 1000000;
	case MAX30131_FSR_2000NA:
		return 2000000;
	default:
		return 0; /* 110/111 Reserved */
	}
}

bool max30131_fsr_uses_fast_clock(max30131_fsr_t fsr)
{
	/* datasheet 0x24 说明:FSR 码 >3 时全部通道用 4× 时钟 */
	return (uint8_t)fsr > 3u;
}

int32_t max30131_lsb_fa(max30131_fsr_t fsr)
{
	int32_t fsr_pa = max30131_fsr_pa(fsr);

	if (fsr_pa == 0) {
		return 0;
	}
	return (int32_t)div_round((int64_t)fsr_pa * 1000, 65536);
}

/* ------------------------------------------------------------------ */
/* offset                                                             */
/* ------------------------------------------------------------------ */
int32_t max30131_offset_pa(max30131_offset_sel_t sel, max30131_fsr_t fsr)
{
	int32_t fsr_pa = max30131_fsr_pa(fsr);

	switch (sel) {
	case MAX30131_OFFSET_0PCT:
		return 0;
	case MAX30131_OFFSET_10PCT_FSR:
		return (int32_t)div_round(fsr_pa, 10);
	case MAX30131_OFFSET_20PCT_FSR:
		return (int32_t)div_round((int64_t)fsr_pa * 2, 10);
	case MAX30131_OFFSET_50PCT_FSR:
		return (int32_t)div_round(fsr_pa, 2);
	case MAX30131_OFFSET_SEL4_9NA:
		return 9000; /* datasheet typ 9nA(7–11),**不是** 10nA */
	case MAX30131_OFFSET_SEL5_19NA:
		return 19000; /* datasheet typ 19nA(16–22),**不是** 20nA */
	case MAX30131_OFFSET_SEL6_40NA:
		return 40000;
	case MAX30131_OFFSET_SEL7_80NA:
		return 80000;
	default:
		return 0;
	}
}

void max30131_offset_range_pa(max30131_offset_sel_t sel, max30131_fsr_t fsr,
			      int32_t *min_pa, int32_t *max_pa)
{
	int32_t fsr_pa = max30131_fsr_pa(fsr);
	int32_t lo = 0, hi = 0;

	switch (sel) {
	case MAX30131_OFFSET_0PCT:
		break;
	case MAX30131_OFFSET_10PCT_FSR: /*  9–11 %FS */
		lo = (int32_t)div_round((int64_t)fsr_pa * 9, 100);
		hi = (int32_t)div_round((int64_t)fsr_pa * 11, 100);
		break;
	case MAX30131_OFFSET_20PCT_FSR: /* 18–22 %FS */
		lo = (int32_t)div_round((int64_t)fsr_pa * 18, 100);
		hi = (int32_t)div_round((int64_t)fsr_pa * 22, 100);
		break;
	case MAX30131_OFFSET_50PCT_FSR: /* 45–55 %FS */
		lo = (int32_t)div_round((int64_t)fsr_pa * 45, 100);
		hi = (int32_t)div_round((int64_t)fsr_pa * 55, 100);
		break;
	case MAX30131_OFFSET_SEL4_9NA:
		lo = 7000;
		hi = 11000;
		break;
	case MAX30131_OFFSET_SEL5_19NA:
		lo = 16000;
		hi = 22000;
		break;
	case MAX30131_OFFSET_SEL6_40NA:
		lo = 34000;
		hi = 46000;
		break;
	case MAX30131_OFFSET_SEL7_80NA:
		lo = 67000;
		hi = 93000;
		break;
	default:
		break;
	}
	if (min_pa) {
		*min_pa = lo;
	}
	if (max_pa) {
		*max_pa = hi;
	}
}

int32_t max30131_max_reduction_pa(int32_t offset_pa)
{
	return offset_pa > 0 ? offset_pa : 0;
}

int32_t max30131_max_oxidation_pa(int32_t fsr_pa, int32_t offset_pa)
{
	int32_t v = fsr_pa - offset_pa;

	return v > 0 ? v : 0;
}

uint8_t max30131_saturation_flags(uint16_t counts, uint16_t margin_counts)
{
	uint8_t f = 0;

	if (counts <= margin_counts) {
		f |= MAX30131_SAT_LOW;
	}
	if ((uint32_t)counts + margin_counts >= 65535u) {
		f |= MAX30131_SAT_HIGH;
	}
	return f;
}

max30131_err_t max30131_check_offset_covers_signal(int32_t offset_pa,
						   int32_t signal_peak_pa,
						   int32_t margin_pct)
{
	int64_t need;

	if (signal_peak_pa < 0 || margin_pct < 0) {
		return MAX30131_ERR_ARG;
	}
	need = div_round((int64_t)signal_peak_pa * (100 + margin_pct), 100);
	if ((int64_t)offset_pa < need) {
		return MAX30131_ERR_RANGE;
	}
	return MAX30131_OK;
}

/* ------------------------------------------------------------------ */
/* counts ↔ 电流                                                       */
/* ------------------------------------------------------------------ */
int32_t max30131_counts_to_iwe_pa(uint16_t counts, int32_t fsr_pa,
				  int32_t offset_pa)
{
	int64_t scaled = div_round((int64_t)counts * fsr_pa, 65536);

	return (int32_t)(scaled - offset_pa);
}

int32_t max30131_counts_to_reduction_pa(uint16_t counts, int32_t fsr_pa,
					int32_t offset_pa)
{
	return -max30131_counts_to_iwe_pa(counts, fsr_pa, offset_pa);
}

int32_t max30131_counts_to_reduction_fa(uint16_t counts, int32_t fsr_pa,
					int32_t offset_pa)
{
	/*
	 * I_red = offset − counts×FSR/2^16,单位 fA。
	 * 先把 offset 与 FSR 都升到 fA(×1000)再做除法,避免先除后乘丢掉
	 * 亚 pA 位 —— 这正是本函数存在的理由(见 .h 注释)。
	 */
	int64_t off_fa = (int64_t)offset_pa * 1000;
	int64_t term_fa = div_round((int64_t)counts * (int64_t)fsr_pa * 1000, 65536);

	return (int32_t)(off_fa - term_fa);
}

uint16_t max30131_reduction_pa_to_counts(int32_t reduction_pa, int32_t fsr_pa,
					int32_t offset_pa)
{
	int64_t counts;

	if (fsr_pa <= 0) {
		return 0;
	}
	/* counts = (offset − I_red) × 2^16 / FSR */
	counts = div_round(((int64_t)offset_pa - reduction_pa) * 65536, fsr_pa);
	if (counts < 0) {
		return 0;
	}
	if (counts > 65535) {
		return 65535;
	}
	return (uint16_t)counts;
}

/* ------------------------------------------------------------------ */
/* 基准 / DAC                                                          */
/* ------------------------------------------------------------------ */
int32_t max30131_ref_mv(max30131_ref_val_t ref)
{
	switch (ref) {
	case MAX30131_REF_1536MV:
		return 1536;
	case MAX30131_REF_2048MV:
		return 2048;
	case MAX30131_REF_3072MV:
		return 3072;
	case MAX30131_REF_4096MV:
		return 4096;
	default:
		return 0;
	}
}

max30131_err_t max30131_check_ref_vs_vdd(max30131_ref_val_t ref, int32_t vdd_mv)
{
	int32_t ref_mv = max30131_ref_mv(ref);

	if (ref_mv == 0) {
		return MAX30131_ERR_ARG;
	}
	if (vdd_mv < ref_mv + MAX30131_VDD_OVER_REF_MIN_MV) {
		return MAX30131_ERR_HEADROOM;
	}
	return MAX30131_OK;
}

max30131_err_t max30131_dac_code_from_mv(int32_t mv, int32_t vref_mv,
					 uint16_t *code_out)
{
	int64_t code;

	if (code_out == NULL || vref_mv <= 0 || mv < 0) {
		return MAX30131_ERR_ARG;
	}
	code = div_round((int64_t)mv * MAX30131_DAC_FULL_SCALE, vref_mv);
	if (code > MAX30131_DAC_CODE_MAX) {
		return MAX30131_ERR_RANGE;
	}
	/* datasheet p32:传输函数只在 CODE > 18 LSB 时成立 */
	if (code < MAX30131_DAC_CODE_LINEAR_MIN) {
		return MAX30131_ERR_RANGE;
	}
	*code_out = (uint16_t)code;
	return MAX30131_OK;
}

int32_t max30131_dac_mv_from_code(uint16_t code, int32_t vref_mv)
{
	if (vref_mv <= 0) {
		return 0;
	}
	return (int32_t)div_round((int64_t)code * vref_mv,
				  MAX30131_DAC_FULL_SCALE);
}

/* ------------------------------------------------------------------ */
/* 极化                                                                */
/* ------------------------------------------------------------------ */
max30131_err_t max30131_polarization_from_e(int32_t v_we_mv, int32_t e_mv,
					    int32_t vref_mv,
					    max30131_polarization_t *out)
{
	max30131_err_t rc;
	int32_t v_re_mv;

	if (out == NULL) {
		return MAX30131_ERR_ARG;
	}
	/* E = V_WE − V_RE ⇒ V_RE = V_WE − E */
	v_re_mv = v_we_mv - e_mv;
	if (v_re_mv < 0) {
		return MAX30131_ERR_RANGE; /* DAC 单极性,取不了负 */
	}

	out->v_dac_a_mv = v_we_mv;
	out->v_dac_b_mv = v_re_mv;

	rc = max30131_dac_code_from_mv(v_we_mv, vref_mv, &out->code_a);
	if (rc != MAX30131_OK) {
		return rc;
	}
	return max30131_dac_code_from_mv(v_re_mv, vref_mv, &out->code_b);
}

int32_t max30131_we_max_mv(int32_t vdd_mv)
{
	return vdd_mv - MAX30131_WE_HEADROOM_MV;
}

max30131_err_t max30131_check_headroom(const max30131_polarization_t *p,
				       int32_t vdd_mv)
{
	int32_t limit;

	if (p == NULL) {
		return MAX30131_ERR_ARG;
	}
	limit = max30131_we_max_mv(vdd_mv);
	if (p->v_dac_a_mv > limit || p->v_dac_b_mv > limit) {
		return MAX30131_ERR_HEADROOM;
	}
	return MAX30131_OK;
}

/* ------------------------------------------------------------------ */
/* FIFO                                                                */
/* ------------------------------------------------------------------ */
max30131_err_t max30131_fifo_unpack(const uint8_t bytes[3],
				    max30131_fifo_word_t *out)
{
	uint32_t raw;
	uint8_t tag4;

	if (bytes == NULL || out == NULL) {
		return MAX30131_ERR_ARG;
	}
	/* Table 7/8:byte1[4:0]=F20..F16, byte2=F15..F8, byte3=F7..F0 */
	raw = ((uint32_t)bytes[0] << 16) | ((uint32_t)bytes[1] << 8) |
	      (uint32_t)bytes[2];
	raw &= MAX30131_FIFO_WORD_MASK;

	out->auto_mode = ((raw >> MAX30131_FIFO_AUTO_Pos) & 1u) != 0u;
	tag4 = (uint8_t)((raw >> MAX30131_FIFO_TAG4_Pos) & 0x0Fu);

	if (tag4 <= MAX30131_FIFO_TAG4_THRESHOLD) {
		out->tag_is_8bit = false;
		out->tag = tag4;
		out->counts = (uint16_t)(raw & 0xFFFFu);
	} else {
		out->tag_is_8bit = true;
		out->tag = (uint8_t)((raw >> MAX30131_FIFO_TAG8_Pos) & 0xFFu);
		out->counts = (uint16_t)(raw & 0x0FFFu);
	}

	if (out->tag_is_8bit && out->tag == MAX30131_FIFO_TAG_EMPTY) {
		return MAX30131_ERR_FIFO_EMPTY;
	}
	return MAX30131_OK;
}

max30131_err_t max30131_fifo_read_s1_reduction_pa(const uint8_t bytes[3],
						  int32_t fsr_pa,
						  int32_t offset_pa,
						  int32_t *pa_out)
{
	max30131_fifo_word_t w;
	max30131_err_t rc;

	if (pa_out == NULL) {
		return MAX30131_ERR_ARG;
	}
	rc = max30131_fifo_unpack(bytes, &w);
	if (rc != MAX30131_OK) {
		return rc;
	}
	if (w.tag_is_8bit || w.tag != MAX30131_FIFO_TAG_S1_DC) {
		return MAX30131_ERR_FIFO_TAG;
	}
	*pa_out = max30131_counts_to_reduction_pa(w.counts, fsr_pa, offset_pa);
	return MAX30131_OK;
}

uint8_t max30131_fifo_a_full_from_batch(uint16_t samples_per_batch)
{
	if (samples_per_batch == 0u) {
		samples_per_batch = 1u; /* a_full=255 ⇒ 1 样本即中断 */
	}
	if (samples_per_batch >= MAX30131_FIFO_DEPTH) {
		return 0u; /* a_full=0 ⇒ 满 256 才中断 */
	}
	return (uint8_t)(MAX30131_FIFO_DEPTH - samples_per_batch);
}

uint16_t max30131_fifo_batch_from_a_full(uint8_t a_full)
{
	return (uint16_t)(MAX30131_FIFO_DEPTH - (uint16_t)a_full);
}

uint16_t max30131_fifo_available(uint8_t ovf_counter, uint16_t data_count)
{
	if ((ovf_counter & 0x7Fu) != 0u) {
		return MAX30131_FIFO_DEPTH; /* 已丢数 */
	}
	if (data_count > MAX30131_FIFO_DEPTH) {
		return MAX30131_FIFO_DEPTH;
	}
	return data_count;
}

/* ------------------------------------------------------------------ */
/* 时序表(datasheet 0x24 两张表 + 0x80 一张表,单位 ms)               */
/* ------------------------------------------------------------------ */
/* 慢钟组(FSR 码 ≤3):CLK_SEL=0 / CLK_SEL=1 */
static const int32_t conv_slow_clk0_ms[11] = {124,   241,   476,	  945,	 1882,
					      3757,  7507,  15007, 30007, 60008,
					      120009};
static const int32_t conv_slow_clk1_ms[11] = {106,   206,   406,	  806,	 1606,
					      3206,  6406,  12806, 25606, 51206,
					      102406};
/* 快钟组(FSR 码 >3) */
static const int32_t conv_fast_clk0_ms[11] = {31,   60,	  119,	 236,	471,
					      939,  1877, 3752, 7502, 15002,
					      30002};
static const int32_t conv_fast_clk1_ms[11] = {27,   52,	  102,	 202,	402,
					      802,  1602, 3202, 6402, 12802,
					      25602};
/* 0xB..0xF 共用最大值 */
static const int32_t conv_max_ms[4] = {240011, 204806, 60003, 51202};

static const uint8_t conv_bits[11] = {12, 13, 14, 15, 16, 16,
				      16, 16, 16, 16, 16};

/* SENS_PERIOD 表恒用慢钟(与 FSR 分组无关) */
static const int32_t period_clk0_ms[11] = {124,	  242,	 476,	945,   1882,
					   3757,  7507,	 15008, 30008, 60008,
					   120009};
static const int32_t period_clk1_ms[11] = {106,	  206,	 406,	806,   1606,
					   3206,  6406,	 12806, 25606, 51206,
					   102406};

int32_t max30131_conv_time_ms(uint8_t conv_time_code, bool clk_sel_40k,
			      bool fast_clock_group)
{
	const int32_t *tbl;

	if (conv_time_code > 0x0Fu) {
		return -1;
	}
	if (conv_time_code > 0x0Au) {
		/* 0xB..0xF 都夹在计数器上限 */
		if (!fast_clock_group) {
			return clk_sel_40k ? conv_max_ms[1] : conv_max_ms[0];
		}
		return clk_sel_40k ? conv_max_ms[3] : conv_max_ms[2];
	}
	if (!fast_clock_group) {
		tbl = clk_sel_40k ? conv_slow_clk1_ms : conv_slow_clk0_ms;
	} else {
		tbl = clk_sel_40k ? conv_fast_clk1_ms : conv_fast_clk0_ms;
	}
	return tbl[conv_time_code];
}

uint8_t max30131_conv_time_bits(uint8_t conv_time_code)
{
	if (conv_time_code > 0x0Au) {
		return 16u;
	}
	return conv_bits[conv_time_code];
}

int32_t max30131_sens_period_ms(uint8_t sens_period_code, bool clk_sel_40k)
{
	if (sens_period_code > 0x0Fu) {
		return -1;
	}
	if (sens_period_code > 0x0Au) {
		return clk_sel_40k ? 204806 : 240011;
	}
	return clk_sel_40k ? period_clk1_ms[sens_period_code]
			   : period_clk0_ms[sens_period_code];
}

max30131_err_t max30131_check_period_vs_conv(uint8_t conv_time_code,
					     uint8_t sens_period_code,
					     bool clk_sel_40k,
					     max30131_fsr_t fsr)
{
	/*
	 * 🔴 用**时钟数**比较,不用 ms 表。ms 表在同码时掩盖真值:
	 * conv 0x0 = 124.20ms、period 0x0 = 124.49ms,两者都舍入成 124,
	 * `124 ≤ 124` 是靠舍入方向侥幸通过的 —— 换个舍入实现就会误判。
	 *
	 * 两个时钟基不同(conv 随 FSR 分组、period 恒用基频),这里不做除法
	 * (整数除会截断),改为把 period 乘上分组倍数,等价且无截断:
	 *   慢钟组:conv_clk ≤ period_clk
	 *   快钟组:conv_clk ≤ period_clk × 4
	 * 溢出安全:period_clk 最大 8388863,×4 = 33555452 < 2^32。
	 */
	uint32_t conv_clk, budget_clk;

	if (conv_time_code > 0x0Fu || sens_period_code > 0x0Fu) {
		return MAX30131_ERR_ARG;
	}
	(void)clk_sel_40k; /* 两侧同基频 ⇒ CLK_SEL 在比较中约掉 */
	conv_clk = max30131_conv_time_clocks(conv_time_code);
	budget_clk = max30131_period_clocks(sens_period_code);
	if (max30131_fsr_uses_fast_clock(fsr)) {
		budget_clk *= 4u;
	}
	if (conv_clk > budget_clk) {
		return MAX30131_ERR_CFG; /* 会置 STATUS1.INVALID_CFG */
	}
	return MAX30131_OK;
}

int max30131_polarization_write_order(const max30131_polarization_t *old_p,
				      const max30131_polarization_t *new_p,
				      int32_t vdd_mv, int32_t vref_mv)
{
	/*
	 * 改 E 要写 DACA + DACB 两对寄存器,物理上必然有中间态(<1ms)。
	 * 中间态的 V_WE/V_RE 是"一个新一个旧"的组合,可能违反 headroom
	 * (WE ≤ VDD−1.1V)。枚举两种次序,返回安全的那个。
	 * 返回 0 = 先写 A,1 = 先写 B,-1 = 两个中间态都不安全(需分两步走中间电位)。
	 */
	int32_t lim_mv;

	if (old_p == NULL || new_p == NULL || vref_mv <= 0) {
		return -1;
	}
	lim_mv = vdd_mv - 1100; /* CP_EN=0 时 WEn 上限 */
	/* 中间态 A:DACA 已新、DACB 仍旧 */
	int32_t we_a = max30131_dac_mv_from_code(new_p->code_a, vref_mv);
	int32_t re_a = max30131_dac_mv_from_code(old_p->code_b, vref_mv);
	/* 中间态 B:DACB 已新、DACA 仍旧 */
	int32_t we_b = max30131_dac_mv_from_code(old_p->code_a, vref_mv);
	int32_t re_b = max30131_dac_mv_from_code(new_p->code_b, vref_mv);
	bool ok_a = we_a <= lim_mv && re_a <= lim_mv && we_a >= 0 && re_a >= 0;
	bool ok_b = we_b <= lim_mv && re_b <= lim_mv && we_b >= 0 && re_b >= 0;

	if (ok_a) {
		return 0;
	}
	if (ok_b) {
		return 1;
	}
	return -1;
}

/* ------------------------------------------------------------------ */
/* 寄存器编码器                                                        */
/* ------------------------------------------------------------------ */
uint8_t max30131_enc_s1_config1(const max30131_s1_config1_t *c)
{
	if (c == NULL) {
		return 0u;
	}
	return BIT_IF(c->we_amp_en, MAX30131_S1C1_WE_AMP_EN_Pos) |
	       BIT_IF(c->ce_amp_en, MAX30131_S1C1_CE_AMP_EN_Pos) |
	       (uint8_t)((c->we_dac_mx & 0x3u) << MAX30131_S1C1_WE_DAC_MX_Pos) |
	       (uint8_t)((c->ce_dac_mx & 0x3u) << MAX30131_S1C1_CE_DAC_MX_Pos) |
	       BIT_IF(c->cp_en, MAX30131_S1C1_CP_EN_Pos) |
	       BIT_IF(c->chop_en, MAX30131_S1C1_CHOP_EN_Pos);
}

uint8_t max30131_enc_s1_config2(const max30131_s1_config2_t *c)
{
	if (c == NULL) {
		return 0u;
	}
	return BIT_IF(c->swb, MAX30131_S1C2_SWB_Pos) |
	       BIT_IF(c->swa, MAX30131_S1C2_SWA_Pos) |
	       BIT_IF(c->sc, MAX30131_S1C2_SC_Pos) |
	       BIT_IF(c->srb, MAX30131_S1C2_SRB_Pos) |
	       BIT_IF(c->sra, MAX30131_S1C2_SRA_Pos) |
	       BIT_IF(c->ilim_en, MAX30131_S1C2_ILIM_EN_Pos) |
	       BIT_IF(c->rs, MAX30131_S1C2_RS_Pos) |
	       BIT_IF(c->swo, MAX30131_S1C2_SWO_Pos);
}

void max30131_switches_3term_we_drive(max30131_s1_config2_t *out)
{
	if (out == NULL) {
		return;
	}
	/* Table 1「3 TERMINAL, WE DRIVE」列:SWA 开 / SWB 闭 / SRA 开 / SRB 闭 / SC 开 */
	out->swa = false;
	out->swb = true;
	out->sc = false;
	out->sra = false;
	out->srb = true;
	out->ilim_en = false;
	out->rs = false; /* 振荡时置 1 串入 60kΩ */
	out->swo = false;
}

uint8_t max30131_enc_s1_config3(bool ios_mode, bool detector_en)
{
	return BIT_IF(ios_mode, MAX30131_S1C3_IOS_MODE_Pos) |
	       BIT_IF(detector_en, MAX30131_S1C3_DETECTOR_EN_Pos);
}

uint8_t max30131_enc_s1_config4(max30131_fsr_t fsr, max30131_offset_sel_t off)
{
	return (uint8_t)(((uint8_t)fsr & 0x7u) << MAX30131_S1C4_FSR_Pos) |
	       (uint8_t)(((uint8_t)off & 0x7u) << MAX30131_S1C4_OFFSET_SEL_Pos);
}

uint8_t max30131_enc_s1_config5(uint8_t conv_time_code, bool select)
{
	return (uint8_t)((conv_time_code & 0x0Fu)
			 << MAX30131_S1C5_CONV_TIME_Pos) |
	       BIT_IF(select, MAX30131_S1C5_SELECT_Pos);
}

/* ================================================================== */
/* 时钟数口径                                                          */
/* ================================================================== */
/*
 * 计数器深度 N = 2^(12+code) − 1(**不是** 2^bits − 1:码 >4 时输出被 decimate
 * 到 16 位,但计数器继续翻倍,所以转换时间继续涨)。上限 8,388,607 = 2^23 − 1
 * ⇒ 码 ≥0xB 全部夹在该值。11 个码逐一与两张 ms 表吻合,见单测。
 */
#define MAX30131_CONV_PRECHARGE_CLOCKS 246u
#define MAX30131_CONV_MAX_CODE 11u

static uint32_t conv_counter_clocks(uint8_t conv_time_code)
{
	uint8_t c = conv_time_code > MAX30131_CONV_MAX_CODE
			    ? (uint8_t)MAX30131_CONV_MAX_CODE
			    : conv_time_code;

	return ((uint32_t)1u << (12u + c)) - 1u;
}

uint32_t max30131_conv_time_clocks(uint8_t conv_time_code)
{
	return conv_counter_clocks(conv_time_code) + MAX30131_CONV_PRECHARGE_CLOCKS;
}

uint32_t max30131_period_clocks(uint8_t sens_period_code)
{
	/* 同码时 period 比 conv 多 10 个时钟 —— 这就是"背靠背"的 0.2% 的来处。 */
	return max30131_conv_time_clocks(sens_period_code) + 10u;
}

int32_t max30131_idle_window_ppm(uint8_t conv_time_code, uint8_t sens_period_code,
				 max30131_fsr_t fsr)
{
	/*
	 * 🔴 两个时钟基不同,必须换算到同一基准:
	 *   conv   随 FSR 分组 —— 快钟组(FSR 码 ≥4)时钟是基频的 4 倍
	 *   period 恒用基频
	 * 折算成基频时钟数:快钟组的 conv 只占基频的 1/4。
	 */
	uint32_t conv = max30131_conv_time_clocks(conv_time_code);
	uint32_t period = max30131_period_clocks(sens_period_code);

	if (max30131_fsr_uses_fast_clock(fsr)) {
		conv = (conv + 3u) / 4u; /* 向上取整:宁可高估占用、低估 idle */
	}
	if (period == 0u) {
		return -1;
	}
	if (conv >= period) {
		return 0; /* 装不下(配置非法),idle 无意义 */
	}
	return (int32_t)(((uint64_t)(period - conv) * 1000000u) / period);
}

/* ================================================================== */
/* 50Hz 抑制表(dB×10,负值)                                          */
/* ================================================================== */
/*
 * 索引 [conv_code][clk40*2 + fast]。离线用 |sinc(50·T_int)| 算好,运行时纯查表 ——
 * prj.conf 已 CBPRINTF_FP_SUPPORT=n,固件里不做 sin/log。
 *
 * 🔴 T 用**积分时间** N/f,不含 246 个 precharge 时钟。判据是我们自己的实测:
 * CONV 0x0→0x1 实测 2.29Hz 谱峰降 23.0dB;积分口径预测 19.1dB、
 * 转换口径预测 30.9dB ⇒ 积分口径才对得上。物理上也只有积分窗做抗混叠。
 *
 * 🔴 WORST 是在 datasheet 给的采样时钟 ±2%(EC 表 f_SLOW)内取最坏。
 * 看 [0][2](慢钟/40kHz/码0):标称 −72.2dB(积分 99.98ms ≈ 4.999 个工频周期,
 * 几乎完美零点)但最坏塌到 −33.8dB。**±2% 的片内振荡器上,靠 sinc 零点压工频
 * 是不成立的**;真正单调改善最坏值的只有加长积分时间(包络 1/(πfT))。
 * 所以选码一律看 WORST,NOM 只用于显示。
 */
static const int16_t rej50_nom_db_x10[11][4] = {
	{  -326,  -133,  -722,  -149 },
	{  -335,  -324,  -783,  -179 },
	{  -375,  -327,  -843,  -843 },
	{  -517,  -336,  -903,  -903 },
	{  -524,  -375,  -963,  -963 },
	{  -554,  -517, -1024, -1024 },
	{  -988,  -524, -1084, -1084 },
	{  -975,  -554, -1144, -1144 },
	{  -969,  -969, -1204, -1204 },
	{  -966,  -966, -1264, -1264 },
	{  -966,  -965, -1325, -1325 },
};
static const int16_t rej50_worst_db_x10[11][4] = {
	{  -279,  -133,  -338,  -144 },
	{  -312,  -272,  -343,  -178 },
	{  -374,  -279,  -362,  -339 },
	{  -433,  -312,  -419,  -344 },
	{  -493,  -374,  -478,  -362 },
	{  -553,  -433,  -539,  -419 },
	{  -612,  -493,  -599,  -478 },
	{  -673,  -553,  -659,  -539 },
	{  -733,  -612,  -719,  -599 },
	{  -793,  -673,  -779,  -659 },
	{  -853,  -733,  -840,  -719 },
};

static int16_t rej50_lookup(const int16_t tbl[11][4], uint8_t conv_time_code,
			    bool clk_sel_40k, bool fast_clock_group)
{
	uint8_t c = conv_time_code > 10u ? 10u : conv_time_code;
	uint8_t col = (uint8_t)((clk_sel_40k ? 2u : 0u) + (fast_clock_group ? 1u : 0u));

	return tbl[c][col];
}

int16_t max30131_rej50_db_x10(uint8_t conv_time_code, bool clk_sel_40k,
			      bool fast_clock_group)
{
	return rej50_lookup(rej50_nom_db_x10, conv_time_code, clk_sel_40k,
			    fast_clock_group);
}

int16_t max30131_rej50_worst_db_x10(uint8_t conv_time_code, bool clk_sel_40k,
				    bool fast_clock_group)
{
	return rej50_lookup(rej50_worst_db_x10, conv_time_code, clk_sel_40k,
			    fast_clock_group);
}

int max30131_auto_conv_code(max30131_fsr_t fsr, uint8_t sens_period_code,
			    bool clk_sel_40k, int *alt_out)
{
	int best = -1, alt = -1;

	if (alt_out != NULL) {
		*alt_out = -1;
	}
	/*
	 * 策略 = **取能装下的最大码**。
	 * 这不是偷懒:最坏 50Hz 抑制、位数、idle 窗口三个排序键在本器件上**同向单调**
	 * (最坏抑制随码单调改善,已在单测里对全部 4 个时钟组合 × 11 码枚举验证),
	 * 所以"字典序三键排序"与"最大码"给出同一个答案,而后者少一个排序循环、
	 * 且不可能因表写错而选出坏码。单测 test_auto_conv_equals_three_key_sort 钉死等价性。
	 */
	for (uint8_t c = 0; c <= MAX30131_CONV_MAX_CODE; c++) {
		if (max30131_check_period_vs_conv(c, sens_period_code, clk_sel_40k,
						  fsr) != MAX30131_OK) {
			continue;
		}
		alt = best;
		best = (int)c;
	}
	if (alt_out != NULL) {
		*alt_out = alt;
	}
	return best;
}

int32_t max30131_sysadc_budget_ms(uint8_t n_channels, bool sys_conv_type)
{
	/*
	 * System ADC 单次转换 8.5ms(EC 表)。SYS_CONV_TYPE=0 ⇒ 每通道 offset+signal
	 * 两次;=1 ⇒ 每**类别**共享一次 offset(本设计四路同属 sensor 类)。
	 * 结果向上取整到 ms,宁可高估预算。
	 */
	const int32_t conv_x10 = 85; /* 8.5ms ×10 */
	int32_t n = n_channels;
	int32_t convs;

	if (n <= 0) {
		return 0;
	}
	convs = sys_conv_type ? (n + 1) : (n * 2);
	return (convs * conv_x10 + 9) / 10;
}

uint8_t max30131_enc_sys_adc_setup(uint8_t sensv_gain_code)
{
	/*
	 * AIN/PWR 增益本设计不用,留复位默认 00;OPA_BYPASS_EN 强制 0 ——
	 * 置 1 会旁路输入缓冲,datasheet 要求信号能驱动 14MΩ,等于在 WE/RE 上
	 * 挂一条 ~29nA 的漏电路径,会破坏被测对象本身(尤其 RE 绝不能带载)。
	 */
	return (uint8_t)((sensv_gain_code & 0x3u)
			 << MAX30131_SYSADC_SENSV_GAIN_Pos);
}

int32_t max30131_sys_adc_mv(uint16_t code, int32_t vref_mv, uint8_t gain_code)
{
	/* V = code/4096 × VREF / gain。gain = 2 / 1 / 0.5 / 0.25 ⇒ 用整数比避免浮点。 */
	static const int32_t num[4] = { 1, 1, 2, 4 }; /* 1/gain 的分子 */
	static const int32_t den[4] = { 2, 1, 1, 1 }; /* 1/gain 的分母 */

	if (vref_mv <= 0) {
		return 0;
	}
	gain_code &= 0x3u;
	return (int32_t)div_round((int64_t)(code & 0x0FFFu) * vref_mv
					  * num[gain_code],
				  (int64_t)4096 * den[gain_code]);
}

uint8_t max30131_enc_reference_control(max30131_ref_val_t ref, bool ref_en,
				       bool external)
{
	return (uint8_t)(((uint8_t)ref & 0x3u) << MAX30131_REFCTL_REF_VAL_Pos) |
	       BIT_IF(ref_en, MAX30131_REFCTL_REF_EN_Pos) |
	       BIT_IF(external, MAX30131_REFCTL_REF_MODE_Pos);
}

uint8_t max30131_enc_convert_setup1(bool eis, uint8_t ioffset_conv,
				    bool sys_conv, uint8_t sens_period_code)
{
	return BIT_IF(eis, MAX30131_CS1_SENS_CONV_TYPE_Pos) |
	       (uint8_t)((ioffset_conv & 0x3u) << MAX30131_CS1_IOFFSET_CONV_Pos) |
	       BIT_IF(sys_conv, MAX30131_CS1_SYS_CONV_TYPE_Pos) |
	       (uint8_t)((sens_period_code & 0x0Fu)
			 << MAX30131_CS1_SENS_PERIOD_Pos);
}

uint8_t max30131_enc_convert_start(bool auto_mode, bool convert)
{
	return BIT_IF(auto_mode, MAX30131_CSTART_AUTO_Pos) |
	       BIT_IF(convert, MAX30131_CSTART_CONVERT_Pos);
}

uint8_t max30131_enc_fifo_config2(bool flush, bool stat_clr, bool a_full_type,
				  bool ro)
{
	return BIT_IF(flush, MAX30131_FIFO_CONFIG2_FLUSH_Pos) |
	       BIT_IF(stat_clr, MAX30131_FIFO_CONFIG2_STAT_CLR_Pos) |
	       BIT_IF(a_full_type, MAX30131_FIFO_CONFIG2_A_FULL_TYPE_Pos) |
	       BIT_IF(ro, MAX30131_FIFO_CONFIG2_RO_Pos);
}

uint8_t max30131_enc_int_enable1(bool a_full_en, bool data_rdy_en)
{
	return BIT_IF(a_full_en, MAX30131_INT_EN1_A_FULL_Pos) |
	       BIT_IF(data_rdy_en, MAX30131_INT_EN1_FIFO_DATA_RDY_Pos);
}

uint8_t max30131_enc_system_control(bool reset, bool shdn, bool bypass_ldo,
				    bool clk_sel_40k)
{
	return BIT_IF(reset, MAX30131_SYSCTL_RESET_Pos) |
	       BIT_IF(shdn, MAX30131_SYSCTL_SHDN_Pos) |
	       BIT_IF(bypass_ldo, MAX30131_SYSCTL_BYPASS_LDO_Pos) |
	       BIT_IF(clk_sel_40k, MAX30131_SYSCTL_CLK_SEL_Pos);
}

void max30131_enc_dac(uint16_t code, bool enable, uint8_t *msb, uint8_t *en_lsb)
{
	if (msb == NULL || en_lsb == NULL) {
		return;
	}
	code &= MAX30131_DAC_CODE_MAX;
	/* 0x69: CODE[11:4] */
	*msb = (uint8_t)(code >> 4);
	/* 0x6A: CODE[3:0]<<4 | EN(bit0) */
	*en_lsb = (uint8_t)((code & 0x0Fu) << MAX30131_DAC_ENLSB_CODE_Pos) |
		  BIT_IF(enable, MAX30131_DAC_ENLSB_EN_Pos);
}

uint16_t max30131_dec_dac_code(uint8_t msb, uint8_t en_lsb)
{
	return (uint16_t)(((uint16_t)msb << 4) |
			  ((uint16_t)en_lsb >> MAX30131_DAC_ENLSB_CODE_Pos));
}

/* ------------------------------------------------------------------ */
/* 手动增益校准(datasheet p41)                                        */
/* ------------------------------------------------------------------ */
int32_t max30131_cal_ioffset_pa(uint16_t adc_at_ref_range, int32_t ref_fsr_pa)
{
	return (int32_t)div_round((int64_t)ref_fsr_pa * adc_at_ref_range, 65536);
}

int32_t max30131_reduction_from_counts_diff_fa(uint16_t counts_offset_only,
					       uint16_t counts_signal,
					       int32_t fsr_pa)
{
	/*
	 * 还原电流使 counts 减小,所以 (offset_only − signal) 为正。
	 * 先升到 fA(×1000)再除 2¹⁶,避免先除后乘丢掉亚 pA 位。
	 * 用 int32 差值以支持"信号反向"(counts 反而更大)的情形,结果为负。
	 */
	int32_t d = (int32_t)counts_offset_only - (int32_t)counts_signal;

	return (int32_t)div_round((int64_t)d * (int64_t)fsr_pa * 1000, 65536);
}

int32_t max30131_cal_fsr_pa(int32_t ioffset_pa, uint16_t adc_at_target_range)
{
	if (adc_at_target_range == 0u) {
		return 0;
	}
	return (int32_t)div_round((int64_t)ioffset_pa * 65536,
				  adc_at_target_range);
}
