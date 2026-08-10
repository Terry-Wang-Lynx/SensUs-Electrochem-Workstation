#include "afe_cfg.h"

#include <stdio.h>
#include <string.h>

/* 已知的左旋多巴标定电流峰值(pA),用于 sig_warn。来源:docs/左旋多巴标定/ 的
 * CHI660E 实测,50µM 时 12.80nA。offset 盖不住它 ⇒ 高浓度点会整段撞轨作废。 */
#define AFE_KNOWN_SIGNAL_PEAK_PA 12800
#define AFE_EOL_VDD_MV 2000
#define AFE_IDLE_WARN_PPM 100000 /* 10% */

static const struct {
	afe_rej_code_t code;
	const char *name;
} rej_names[] = {
	{ AFE_REJ_NONE, "none" },
	{ AFE_REJ_TOO_LONG, "too_long" },
	{ AFE_REJ_VERB, "verb" },
	{ AFE_REJ_UNKNOWN_KEY, "unknown_key" },
	{ AFE_REJ_DUP_KEY, "dup_key" },
	{ AFE_REJ_TOO_MANY_KEYS, "too_many_keys" },
	{ AFE_REJ_VALUE, "value" },
	{ AFE_REJ_ARG, "arg" },
	{ AFE_REJ_PERIOD_LT_CONV, "period_lt_conv" },
	{ AFE_REJ_OFFSET_GT_FSR, "offset_gt_fsr" },
	{ AFE_REJ_SYSPER_SHORT, "sysper_short" },
	{ AFE_REJ_DAC, "dac" },
	{ AFE_REJ_DAC_MID, "dac_mid" },
	{ AFE_REJ_PERTURB_DURING_RUN, "perturb_during_run" },
};

const char *afe_rej_name(afe_rej_code_t code)
{
	for (size_t i = 0; i < sizeof(rej_names) / sizeof(rej_names[0]); i++) {
		if (rej_names[i].code == code) {
			return rej_names[i].name;
		}
	}
	return "?";
}

/* ------------------------------------------------------------------ */
/* 解析                                                               */
/* ------------------------------------------------------------------ */
static void set_reject(afe_reject_t *why, afe_rej_code_t code, const char *key,
		       int32_t a, int32_t b)
{
	if (why == NULL) {
		return;
	}
	why->code = code;
	why->a = a;
	why->b = b;
	why->key[0] = '\0';
	if (key != NULL) {
		size_t n = strlen(key);

		if (n >= sizeof(why->key)) {
			n = sizeof(why->key) - 1u;
		}
		memcpy(why->key, key, n);
		why->key[n] = '\0';
	}
}

/* 十进制或 0x 十六进制;允许前导 '-'。成功返回 true。 */
static bool parse_int(const char *s, int32_t *out)
{
	int32_t sign = 1, val = 0;
	int digits = 0;

	if (s == NULL || *s == '\0') {
		return false;
	}
	if (*s == '-') {
		sign = -1;
		s++;
	}
	if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
		s += 2;
		while (*s != '\0') {
			int d;

			if (*s >= '0' && *s <= '9') {
				d = *s - '0';
			} else if (*s >= 'a' && *s <= 'f') {
				d = *s - 'a' + 10;
			} else if (*s >= 'A' && *s <= 'F') {
				d = *s - 'A' + 10;
			} else {
				return false;
			}
			if (val > (2147483647 - d) / 16) {
				return false;
			}
			val = val * 16 + d;
			digits++;
			s++;
		}
	} else {
		while (*s != '\0') {
			if (*s < '0' || *s > '9') {
				return false;
			}
			if (val > (2147483647 - (*s - '0')) / 10) {
				return false;
			}
			val = val * 10 + (*s - '0');
			digits++;
			s++;
		}
	}
	if (digits == 0) {
		return false;
	}
	*out = sign * val;
	return true;
}

/* 键表。`lo/hi` 是合法域;`perturb` 标记会动电解池的键。 */
enum key_id {
	K_FSR, K_OFF, K_CONV, K_PERIOD, K_SYSPER, K_CLK40, K_IOC,
	K_CHOP, K_RS, K_IOS, K_E, K_VWE, K_IDLE, K_CELLV, K_SATPCT,
	K_COUNT
};
static const struct {
	const char *name;
	int32_t lo, hi;
	bool perturb;
} keys[K_COUNT] = {
	[K_FSR]    = { "fsr",    0, 5,    false },
	[K_OFF]    = { "off",    0, 7,    false },
	[K_CONV]   = { "conv",   0, 11,   false }, /* 另接受字面量 "auto" */
	[K_PERIOD] = { "period", 0, 10,   false },
	[K_SYSPER] = { "sysper", 0, 10,   false },
	[K_CLK40]  = { "clk40",  0, 1,    false },
	[K_IOC]    = { "ioc",    0, 3,    false },
	[K_CHOP]   = { "chop",   0, 1,    true },
	[K_RS]     = { "rs",     0, 1,    true },
	[K_IOS]    = { "ios",    0, 1,    true },
	[K_E]      = { "e",   -800, 800,  true },
	[K_VWE]    = { "vwe", 100, 2000,  true },
	[K_IDLE]   = { "idle",   0, 2,    true },
	[K_CELLV]  = { "cellv",  0, 1,    false },
	[K_SATPCT] = { "satpct", 0, 50,   false },
};

static void apply_key(afe_cfg_t *c, enum key_id k, int32_t v, bool is_auto)
{
	switch (k) {
	case K_FSR:    c->fsr = (max30131_fsr_t)v; break;
	case K_OFF:    c->off = (max30131_offset_sel_t)v; break;
	case K_CONV:
		if (is_auto) {
			c->conv_pinned = false;
		} else {
			c->conv = (uint8_t)v;
			c->conv_pinned = true;
		}
		break;
	case K_PERIOD: c->period = (uint8_t)v; break;
	case K_SYSPER: c->sysper = (uint8_t)v; break;
	case K_CLK40:  c->clk40 = v != 0; break;
	case K_IOC:    c->ioc = (uint8_t)v; break;
	case K_CHOP:   c->chop = v != 0; break;
	case K_RS:     c->rs = v != 0; break;
	case K_IOS:    c->ios = v != 0; break;
	case K_E:      c->e_mv = v; break;
	case K_VWE:    c->vwe_mv = v; break;
	case K_IDLE:   c->idle = (afe_idle_mode_t)v; break;
	case K_CELLV:  c->cellv = v != 0; break;
	case K_SATPCT: c->satpct = (uint8_t)v; break;
	default: break;
	}
}

static bool tok_eq(const char *tok, size_t len, const char *lit)
{
	return strlen(lit) == len && strncmp(tok, lit, len) == 0;
}

bool afe_cfg_parse(const char *line, const afe_cfg_t *base, afe_cmd_t *out,
		   afe_reject_t *why)
{
	size_t len;
	const char *p;
	bool seen[K_COUNT] = { false };
	int n_keys = 0;
	int n_bare = 0;
	int32_t bare[2] = { 0, 0 };

	set_reject(why, AFE_REJ_NONE, NULL, 0, 0);
	if (line == NULL || base == NULL || out == NULL) {
		set_reject(why, AFE_REJ_VALUE, NULL, 0, 0);
		return false;
	}
	len = strlen(line);
	if (len >= AFE_CFG_LINE_MAX) {
		set_reject(why, AFE_REJ_TOO_LONG, NULL, (int32_t)len,
			   AFE_CFG_LINE_MAX - 1);
		return false;
	}
	memset(out, 0, sizeof(*out));
	out->cfg = *base;

	p = line;
	while (*p == ' ' || *p == '\t') {
		p++;
	}
	/* 注释与空行:忽略,不算错(collector 已过滤,固件再挡一层) */
	if (*p == '\0' || *p == '#') {
		out->verb = AFE_VERB_NONE;
		return true;
	}

	/* 动词 */
	const char *vb = p;

	while (*p != '\0' && *p != ' ' && *p != '\t') {
		p++;
	}
	size_t vlen = (size_t)(p - vb);
	bool is_range = false;

	if (tok_eq(vb, vlen, "START")) {
		out->verb = AFE_VERB_START;
	} else if (tok_eq(vb, vlen, "STOP")) {
		out->verb = AFE_VERB_STOP;
	} else if (tok_eq(vb, vlen, "GET")) {
		out->verb = AFE_VERB_GET;
	} else if (tok_eq(vb, vlen, "STATUS")) {
		out->verb = AFE_VERB_STATUS;
	} else if (tok_eq(vb, vlen, "SET")) {
		out->verb = AFE_VERB_SET;
	} else if (tok_eq(vb, vlen, "RANGE")) {
		out->verb = AFE_VERB_SET; /* 遗留别名 ≡ SET fsr= off= */
		is_range = true;
	} else if (tok_eq(vb, vlen, "PEEK")) {
		out->verb = AFE_VERB_PEEK;
	} else if (tok_eq(vb, vlen, "POKE")) {
		out->verb = AFE_VERB_POKE;
	} else if (tok_eq(vb, vlen, "OCP")) {
		out->verb = AFE_VERB_OCP;
	} else {
		/* 🔴 A3:未识别动词必须报,不能静默 —— 否则打错命令与命令没送达同形 */
		set_reject(why, AFE_REJ_VERB, NULL, 0, 0);
		return false;
	}

	/* 参数 */
	while (*p != '\0') {
		while (*p == ' ' || *p == '\t') {
			p++;
		}
		if (*p == '\0') {
			break;
		}
		const char *tok = p;

		while (*p != '\0' && *p != ' ' && *p != '\t') {
			p++;
		}
		size_t tlen = (size_t)(p - tok);

		if (tok_eq(tok, tlen, "FORCE")) {
			out->forced = true;
			continue;
		}
		const char *eq = memchr(tok, '=', tlen);

		if (eq == NULL) {
			/* 裸数值:RANGE / PEEK / POKE / OCP 的位置参数 */
			char tmp[24];

			if (tlen >= sizeof(tmp)) {
				set_reject(why, AFE_REJ_VALUE, NULL, 0, 0);
				return false;
			}
			memcpy(tmp, tok, tlen);
			tmp[tlen] = '\0';
			int32_t v;

			if (!parse_int(tmp, &v)) {
				set_reject(why, AFE_REJ_VALUE, NULL, 0, 0);
				return false;
			}
			if (n_bare >= 2) {
				set_reject(why, AFE_REJ_TOO_MANY_KEYS, NULL, n_bare, 2);
				return false;
			}
			bare[n_bare++] = v;
			continue;
		}
		/* k=v。'=' 两侧不允许空格(少一种歧义) */
		size_t klen = (size_t)(eq - tok);
		size_t vlen2 = tlen - klen - 1u;

		if (klen == 0u || vlen2 == 0u) {
			set_reject(why, AFE_REJ_VALUE, NULL, 0, 0);
			return false;
		}
		int found = -1;

		for (int k = 0; k < K_COUNT; k++) {
			if (tok_eq(tok, klen, keys[k].name)) {
				found = k;
				break;
			}
		}
		if (found < 0) {
			char kn[16];
			size_t n = klen < sizeof(kn) - 1u ? klen : sizeof(kn) - 1u;

			memcpy(kn, tok, n);
			kn[n] = '\0';
			set_reject(why, AFE_REJ_UNKNOWN_KEY, kn, 0, 0);
			return false;
		}
		if (seen[found]) {
			/* 🔴 不做 last-wins:同一键出现两次 100% 是笔误 */
			set_reject(why, AFE_REJ_DUP_KEY, keys[found].name, 0, 0);
			return false;
		}
		if (n_keys >= AFE_CFG_MAX_KEYS) {
			set_reject(why, AFE_REJ_TOO_MANY_KEYS, NULL, n_keys,
				   AFE_CFG_MAX_KEYS);
			return false;
		}
		bool is_auto = false;
		int32_t v = 0;

		if (found == K_CONV && tok_eq(eq + 1, vlen2, "auto")) {
			is_auto = true;
		} else {
			char tmp[24];

			if (vlen2 >= sizeof(tmp)) {
				set_reject(why, AFE_REJ_VALUE, keys[found].name, 0, 0);
				return false;
			}
			memcpy(tmp, eq + 1, vlen2);
			tmp[vlen2] = '\0';
			if (!parse_int(tmp, &v)) {
				set_reject(why, AFE_REJ_VALUE, keys[found].name, 0, 0);
				return false;
			}
			if (v < keys[found].lo || v > keys[found].hi) {
				set_reject(why, AFE_REJ_ARG, keys[found].name, v,
					   keys[found].hi);
				return false;
			}
		}
		seen[found] = true;
		n_keys++;
		apply_key(&out->cfg, (enum key_id)found, v, is_auto);
	}

	if (is_range) {
		if (n_bare != 2) {
			set_reject(why, AFE_REJ_VALUE, "range", n_bare, 2);
			return false;
		}
		if (bare[0] < 0 || bare[0] > 5) {
			set_reject(why, AFE_REJ_ARG, "fsr", bare[0], 5);
			return false;
		}
		if (bare[1] < 0 || bare[1] > 7) {
			set_reject(why, AFE_REJ_ARG, "off", bare[1], 7);
			return false;
		}
		apply_key(&out->cfg, K_FSR, bare[0], false);
		apply_key(&out->cfg, K_OFF, bare[1], false);
		n_keys += 2;
	} else {
		out->arg0 = bare[0];
		out->arg1 = bare[1];
	}
	out->n_keys = (uint8_t)n_keys;
	return true;
}

/* ------------------------------------------------------------------ */
/* 派生                                                               */
/* ------------------------------------------------------------------ */
void afe_cfg_derive(afe_cfg_t *cfg, afe_derived_t *out)
{
	if (cfg == NULL || out == NULL) {
		return;
	}
	memset(out, 0, sizeof(*out));
	out->conv_alt = -1;

	/* A1:conv 未钉住时由 auto 策略重算 —— 这是对"写死 CONV_TIME"回归的结构性修复 */
	if (!cfg->conv_pinned) {
		int alt = -1;
		int c = max30131_auto_conv_code(cfg->fsr, cfg->period, cfg->clk40, &alt);

		if (c >= 0) {
			cfg->conv = (uint8_t)c;
			out->conv_alt = alt;
		}
	}

	bool fast = max30131_fsr_uses_fast_clock(cfg->fsr);

	out->fsr_pa = max30131_fsr_pa(cfg->fsr);
	out->off_pa = max30131_offset_pa(cfg->off, cfg->fsr);
	max30131_offset_range_pa(cfg->off, cfg->fsr, &out->off_min_pa, &out->off_max_pa);
	out->bits = max30131_conv_time_bits(cfg->conv);
	out->conv_clk = max30131_conv_time_clocks(cfg->conv);
	out->period_clk = max30131_period_clocks(cfg->period);
	out->conv_ms = max30131_conv_time_ms(cfg->conv, cfg->clk40, fast);
	out->period_ms = max30131_sens_period_ms(cfg->period, cfg->clk40);
	out->idle_ppm = max30131_idle_window_ppm(cfg->conv, cfg->period, cfg->fsr);
	out->lsb_frame_fa = max30131_lsb_fa(cfg->fsr);
	out->lsb_eff_fa = out->lsb_frame_fa << (16u - out->bits);
	out->rej50_db_x10 = max30131_rej50_db_x10(cfg->conv, cfg->clk40, fast);
	out->rej50_worst_db_x10 =
		max30131_rej50_worst_db_x10(cfg->conv, cfg->clk40, fast);
	if (out->conv_alt >= 0) {
		out->conv_alt_db_x10 = max30131_rej50_worst_db_x10(
			(uint8_t)out->conv_alt, cfg->clk40, fast);
	}
	out->red_max_pa = max30131_max_reduction_pa(out->off_pa);
	out->ox_max_pa = max30131_max_oxidation_pa(out->fsr_pa, out->off_pa);

	/* sat 预警余量:按零电流码的 satpct% 取,上限 1311(2%FS,不给已验证配置引回归) */
	if (out->fsr_pa > 0 && out->off_pa > 0) {
		int64_t zero_code = ((int64_t)out->off_pa * 65536) / out->fsr_pa;
		int64_t m = zero_code * cfg->satpct / 100;

		if (m > 1311) {
			m = 1311;
		}
		if (m < 8) {
			m = 8;
		}
		out->sat_margin = (uint16_t)m;
		out->sat_margin_pa =
			(int32_t)(((int64_t)out->sat_margin * out->fsr_pa) / 65536);
	}

	out->sysbudget_ms = max30131_sysadc_budget_ms(cfg->cellv ? 4u : 0u, false);
	out->sysper_ms = max30131_sens_period_ms(cfg->sysper, cfg->clk40);

	(void)max30131_polarization_from_e(cfg->vwe_mv, cfg->e_mv,
					   max30131_ref_mv(MAX30131_REF_1536MV),
					   &out->pol);

	/* 警告位:不拒绝,但必然打印(A3) */
	out->idle_warn = out->idle_ppm > AFE_IDLE_WARN_PPM;
	out->headroom_warn = max30131_check_headroom(&out->pol, AFE_EOL_VDD_MV)
			     != MAX30131_OK;
	out->sig_warn = out->off_min_pa < AFE_KNOWN_SIGNAL_PEAK_PA;
}

/* ------------------------------------------------------------------ */
/* 校验                                                               */
/* ------------------------------------------------------------------ */
bool afe_cfg_validate(const afe_cfg_t *cfg, const afe_derived_t *d,
		      bool acquiring, bool forced, afe_reject_t *why)
{
	set_reject(why, AFE_REJ_NONE, NULL, 0, 0);
	if (cfg == NULL || d == NULL) {
		set_reject(why, AFE_REJ_VALUE, NULL, 0, 0);
		return false;
	}
	if (cfg->fsr > MAX30131_FSR_2000NA || cfg->off > MAX30131_OFFSET_SEL7_80NA ||
	    cfg->conv > 11u || cfg->period > 10u || cfg->sysper > 10u ||
	    cfg->ioc > 3u || cfg->idle > AFE_IDLE_DISCONNECT || cfg->satpct > 50u) {
		set_reject(why, AFE_REJ_ARG, NULL, 0, 0);
		return false;
	}
	if (max30131_check_period_vs_conv(cfg->conv, cfg->period, cfg->clk40,
					  cfg->fsr) != MAX30131_OK) {
		set_reject(why, AFE_REJ_PERIOD_LT_CONV, "conv",
			   (int32_t)d->conv_clk, (int32_t)d->period_clk);
		return false;
	}
	if (d->off_pa > d->fsr_pa) {
		set_reject(why, AFE_REJ_OFFSET_GT_FSR, "off", d->off_pa, d->fsr_pa);
		return false;
	}
	if (cfg->cellv && d->sysbudget_ms > d->sysper_ms) {
		/* p143:总转换时间 > SYS_PERIOD ⇒ INVALID_CFG 且被打断通道的数据无效 */
		set_reject(why, AFE_REJ_SYSPER_SHORT, "sysper", d->sysbudget_ms,
			   d->sysper_ms);
		return false;
	}
	max30131_polarization_t probe;

	if (max30131_polarization_from_e(cfg->vwe_mv, cfg->e_mv,
					 max30131_ref_mv(MAX30131_REF_1536MV),
					 &probe) != MAX30131_OK) {
		/* V_RE = V_WE − E < 0(DAC 单极性取不了负)或超基准域 */
		set_reject(why, AFE_REJ_DAC, "e", cfg->e_mv, cfg->vwe_mv);
		return false;
	}
	/*
	 * 「采集中不许扰动电解池」这条**不在这里判** —— 它需要比较前后配置,
	 * 而那是 plan 的职责(plan->perturbs_cell)。调用方在 plan 之后判:
	 *   if (acquiring && plan.perturbs_cell && !forced) ⇒ perturb_during_run
	 * 这两个参数留在签名里,是为了让"这一层刻意不管它"在类型上可见。
	 */
	(void)acquiring;
	(void)forced;
	return true;
}

/* ------------------------------------------------------------------ */
/* 写序                                                               */
/* ------------------------------------------------------------------ */
static void push(afe_plan_t *p, uint8_t addr, uint8_t val, uint8_t before)
{
	if (val == before) {
		p->skipped++; /* 少一次 SPI ⇒ 少一个中间态、少一次电磁扰动 */
		return;
	}
	if (p->n >= AFE_CFG_MAX_WRITES) {
		return;
	}
	p->w[p->n].addr = addr;
	p->w[p->n].val = val;
	p->n++;
}

static uint8_t byte_20(const afe_cfg_t *c)
{
	const max30131_s1_config1_t s = {
		.we_amp_en = c->amps_on, .ce_amp_en = c->amps_on,
		.we_dac_mx = MAX30131_DAC_MX_A, .ce_dac_mx = MAX30131_DAC_MX_B,
		.cp_en = false, .chop_en = c->chop,
	};

	return max30131_enc_s1_config1(&s);
}

static uint8_t byte_21(const afe_cfg_t *c)
{
	max30131_s1_config2_t s;

	max30131_switches_3term_we_drive(&s);
	s.rs = c->rs;
	return max30131_enc_s1_config2(&s);
}

static uint8_t byte_80(const afe_cfg_t *c)
{
	return max30131_enc_convert_setup1(false, c->ioc, false, c->period);
}

void afe_cfg_plan(const afe_cfg_t *old_cfg, const afe_derived_t *old_d,
		  const afe_cfg_t *new_cfg, const afe_derived_t *new_d,
		  afe_plan_t *out)
{
	if (out == NULL || old_cfg == NULL || new_cfg == NULL) {
		return;
	}
	memset(out, 0, sizeof(*out));
	(void)old_d;
	(void)new_d;

	out->perturbs_cell = old_cfg->chop != new_cfg->chop ||
			     old_cfg->rs != new_cfg->rs ||
			     old_cfg->ios != new_cfg->ios ||
			     old_cfg->e_mv != new_cfg->e_mv ||
			     old_cfg->vwe_mv != new_cfg->vwe_mv ||
			     old_cfg->idle != new_cfg->idle;

	uint32_t p_old = max30131_period_clocks(old_cfg->period);
	uint32_t p_new = max30131_period_clocks(new_cfg->period);
	bool widen_first = p_new > p_old;

	/*
	 * 🔴 「松的先写,紧的后写」。period 变大时先放宽窗口,变小时最后收紧;
	 * 中间的 conv 对按"较短中间态优先"排序。这样任意合法起点→合法终点的
	 * **每一个前缀**都满足 conv ≤ period(全组合枚举单测背书)。
	 */
	if (widen_first) {
		push(out, MAX30131_REG_CONVERT_SETUP1, byte_80(new_cfg),
		     byte_80(old_cfg));
	}

	/* conv 对:0x23(FSR+offset)与 0x24(CONV_TIME+SELECT)。
	 * 先写哪个取决于哪个中间态的转换时间更短。 */
	uint8_t b23_new = max30131_enc_s1_config4(new_cfg->fsr, new_cfg->off);
	uint8_t b23_old = max30131_enc_s1_config4(old_cfg->fsr, old_cfg->off);
	/* 🔴 SELECT 必须来自配置的运行态,**不能硬编码 true** ——
	 * 原 apply_range() 写死 true,idle(sensor 已 deselect)期间收到 RANGE
	 * 会静默重新选中 sensor、让电流转换在开路态跑起来。 */
	uint8_t b24_new = max30131_enc_s1_config5(new_cfg->conv,
						  new_cfg->sensor_selected);
	uint8_t b24_old = max30131_enc_s1_config5(old_cfg->conv,
						  old_cfg->sensor_selected);

	/* 中间态 A = 新 FSR + 旧 conv;中间态 B = 旧 FSR + 新 conv */
	uint32_t mid_a = max30131_conv_time_clocks(old_cfg->conv) *
			 (max30131_fsr_uses_fast_clock(new_cfg->fsr) ? 1u : 4u);
	uint32_t mid_b = max30131_conv_time_clocks(new_cfg->conv) *
			 (max30131_fsr_uses_fast_clock(old_cfg->fsr) ? 1u : 4u);

	if (mid_a <= mid_b) {
		push(out, MAX30131_REG_S1_CONFIG4, b23_new, b23_old);
		push(out, MAX30131_REG_S1_CONFIG5, b24_new, b24_old);
	} else {
		push(out, MAX30131_REG_S1_CONFIG5, b24_new, b24_old);
		push(out, MAX30131_REG_S1_CONFIG4, b23_new, b23_old);
	}

	if (!widen_first) {
		push(out, MAX30131_REG_CONVERT_SETUP1, byte_80(new_cfg),
		     byte_80(old_cfg));
	}

	/* 其余寄存器与 conv/period 不变式无关,次序不敏感 */
	push(out, MAX30131_REG_S1_CONFIG1, byte_20(new_cfg), byte_20(old_cfg));
	push(out, MAX30131_REG_S1_CONFIG2, byte_21(new_cfg), byte_21(old_cfg));
	push(out, MAX30131_REG_S1_CONFIG3,
	     max30131_enc_s1_config3(new_cfg->ios, false),
	     max30131_enc_s1_config3(old_cfg->ios, false));

	/* System ADC:通道变多 ⇒ 先放宽 sysper;变少 ⇒ 先关通道再收紧 */
	const uint8_t sel2_all = (uint8_t)((1u << MAX30131_SYSADC_S1_WE_SEL_Pos) |
					   (1u << MAX30131_SYSADC_S1_RE_SEL_Pos) |
					   (1u << MAX30131_SYSADC_S1_CE_SEL_Pos) |
					   (1u << MAX30131_SYSADC_S1_WO_SEL_Pos));
	uint8_t sel2_new = new_cfg->cellv ? sel2_all : 0u;
	uint8_t sel2_old = old_cfg->cellv ? sel2_all : 0u;
	uint8_t sel1_new = new_cfg->cellv
				   ? (uint8_t)(1u << MAX30131_SYSADC_SYS_SELECT_Pos) : 0u;
	uint8_t sel1_old = old_cfg->cellv
				   ? (uint8_t)(1u << MAX30131_SYSADC_SYS_SELECT_Pos) : 0u;
	uint8_t b81_new = (uint8_t)(new_cfg->sysper & 0x0Fu);
	uint8_t b81_old = (uint8_t)(old_cfg->sysper & 0x0Fu);

	if (new_cfg->cellv && !old_cfg->cellv) {
		push(out, MAX30131_REG_CONVERT_SETUP2, b81_new, b81_old);
		push(out, MAX30131_REG_SYS_ADC_IN_SEL2, sel2_new, sel2_old);
		push(out, MAX30131_REG_SYS_ADC_IN_SEL1, sel1_new, sel1_old);
	} else {
		push(out, MAX30131_REG_SYS_ADC_IN_SEL1, sel1_new, sel1_old);
		push(out, MAX30131_REG_SYS_ADC_IN_SEL2, sel2_new, sel2_old);
		push(out, MAX30131_REG_CONVERT_SETUP2, b81_new, b81_old);
	}
}

/* ------------------------------------------------------------------ */
/* 审计行格式化                                                        */
/* ------------------------------------------------------------------ */
size_t afe_cfg_fmt_applied(uint32_t ep, int64_t ms, const char *src,
			   uint8_t nlines, bool forced, const afe_plan_t *plan,
			   const afe_cfg_t *o, const afe_cfg_t *c,
			   char *buf, size_t n)
{
	int w;

	if (buf == NULL || o == NULL || c == NULL || plan == NULL) {
		return 0;
	}
	w = snprintf(buf, n,
		"CFG_APPLIED ep=%u ms=%lld src=%s nlines=%u forced=%d perturbs_cell=%d "
		"nregs=%u skipped=%u "
		"fsr=%d fsr0=%d off=%d off0=%d conv=%u conv0=%u conv_src=%s "
		"period=%u period0=%u sysper=%u sysper0=%u clk40=%d clk400=%d "
		"e_mv=%d e_mv0=%d vwe_mv=%d vwe_mv0=%d ioc=%u ioc0=%u "
		"idle=%d idle0=%d cellv=%d cellv0=%d chop=%d chop0=%d "
		"rs=%d rs0=%d ios=%d ios0=%d sel=%d sel0=%d amps=%d amps0=%d",
		ep, (long long)ms, src == NULL ? "?" : src, nlines, forced ? 1 : 0,
		plan->perturbs_cell ? 1 : 0, plan->n, plan->skipped,
		(int)c->fsr, (int)o->fsr, (int)c->off, (int)o->off,
		c->conv, o->conv, c->conv_pinned ? "pin" : "auto",
		c->period, o->period, c->sysper, o->sysper,
		c->clk40 ? 1 : 0, o->clk40 ? 1 : 0,
		c->e_mv, o->e_mv, c->vwe_mv, o->vwe_mv, c->ioc, o->ioc,
		(int)c->idle, (int)o->idle, c->cellv ? 1 : 0, o->cellv ? 1 : 0,
		c->chop ? 1 : 0, o->chop ? 1 : 0, c->rs ? 1 : 0, o->rs ? 1 : 0,
		c->ios ? 1 : 0, o->ios ? 1 : 0,
		c->sensor_selected ? 1 : 0, o->sensor_selected ? 1 : 0,
		c->amps_on ? 1 : 0, o->amps_on ? 1 : 0);
	return (w > 0 && (size_t)w < n) ? (size_t)w : 0u;
}

size_t afe_cfg_fmt_derived(uint32_t ep, const afe_cfg_t *cfg,
			   const afe_derived_t *d, char *buf, size_t n)
{
	int w;

	if (buf == NULL || cfg == NULL || d == NULL) {
		return 0;
	}
	w = snprintf(buf, n,
		"CFG_DERIVED ep=%u fsr_pa=%d off_pa=%d off_min_pa=%d off_max_pa=%d "
		"bits=%u conv_clk=%u period_clk=%u conv_ms=%d period_ms=%d "
		"idle_ppm=%d lsb_frame_fa=%d lsb_eff_fa=%d "
		"rej50_db_x10=%d rej50_worst_db_x10=%d conv_alt=%d conv_alt_db_x10=%d "
		"red_max_pa=%d ox_max_pa=%d sat_margin=%u sat_margin_pa=%d "
		"sysbudget_ms=%d sysper_ms=%d daca=%u dacb=%u "
		"idle_warn=%d headroom_warn=%d sig_warn=%d",
		ep, d->fsr_pa, d->off_pa, d->off_min_pa, d->off_max_pa,
		d->bits, d->conv_clk, d->period_clk, d->conv_ms, d->period_ms,
		d->idle_ppm, d->lsb_frame_fa, d->lsb_eff_fa,
		d->rej50_db_x10, d->rej50_worst_db_x10, d->conv_alt,
		d->conv_alt_db_x10, d->red_max_pa, d->ox_max_pa,
		d->sat_margin, d->sat_margin_pa, d->sysbudget_ms, d->sysper_ms,
		d->pol.code_a, d->pol.code_b,
		d->idle_warn ? 1 : 0, d->headroom_warn ? 1 : 0, d->sig_warn ? 1 : 0);
	return (w > 0 && (size_t)w < n) ? (size_t)w : 0u;
}

size_t afe_cfg_fmt_reg(uint32_t ep, uint8_t i, uint8_t total, uint8_t addr,
		       uint8_t before, uint8_t after, uint8_t readback,
		       char *buf, size_t n)
{
	int w;

	if (buf == NULL) {
		return 0;
	}
	w = snprintf(buf, n,
		     "CFG_REG ep=%u i=%u n=%u addr=0x%02X before=0x%02X after=0x%02X "
		     "readback=0x%02X ok=%d",
		     ep, i, total, addr, before, after, readback,
		     readback == after ? 1 : 0);
	return (w > 0 && (size_t)w < n) ? (size_t)w : 0u;
}

size_t afe_cfg_fmt_reject(uint32_t ep, int64_t ms, const afe_reject_t *why,
			  const char *raw, char *buf, size_t n)
{
	char safe[41];
	int w;

	if (buf == NULL || why == NULL) {
		return 0;
	}
	/* raw 截断到 40 字符,并把空白换成 '_' —— 保持 key=value 单行可解析 */
	size_t i = 0;

	if (raw != NULL) {
		for (; raw[i] != '\0' && i < sizeof(safe) - 1u; i++) {
			char ch = raw[i];

			safe[i] = (ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n')
					  ? '_' : ch;
		}
	}
	safe[i] = '\0';
	w = snprintf(buf, n,
		     "CFG_REJECT ep=%u ms=%lld reason=%s key=%s a=%d b=%d raw=%s",
		     ep, (long long)ms, afe_rej_name(why->code),
		     why->key[0] != '\0' ? why->key : "-", why->a, why->b, safe);
	return (w > 0 && (size_t)w < n) ? (size_t)w : 0u;
}
