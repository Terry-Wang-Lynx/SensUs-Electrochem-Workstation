/*
 * afe_cfg —— 运行时 AFE 配置的解析 / 派生 / 校验 / 写序 / 审计格式化
 *
 * 为什么单独成模块:这里**全部是纯函数**,不碰 SPI、不碰 Zephyr,可以在开发机上
 * 用 tests/minitest.h 直接跑。命令协议与写序定理是本设计最容易出错、也最值得
 * 机器证明的部分(项目被 STATUS1.INVALID_CFG 静默坑过两次),放进纯函数层才测得动。
 * main.c 只保留 afe_cfg_commit() —— 执行 plan + 回读 + confirm + 回滚。
 *
 * 四条设计公理(每个决策都回溯到它们):
 *   A1 派生优于设定:能算出来的量不做独立旋钮(CONV_TIME 的历史 bug 正因它被当成
 *      独立参数才可能发生)。
 *   A2 全有或全无:一行命令 = 一个事务,中间态不得出现在器件上。
 *   A3 沉默即故障:未识别命令、被丢弃的审计行、未确认的配置,全部必须有输出。
 *      "什么都没打"不允许与"一切正常"同形。
 *   A4 器件是权威:校验通过 ≠ 芯片同意。回读 + 复核 STATUS1,不一致就回滚并报
 *      "模型错了"。
 */
#ifndef AFE_CFG_H
#define AFE_CFG_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "max30131.h"

/* 命令行长度上限。最长实际命令(全部键 + FORCE)约 111 字符。 */
#define AFE_CFG_LINE_MAX 128
#define AFE_CFG_MAX_KEYS 16
/* plan 里最多要写的寄存器数:0x14 0x20 0x21 0x22 0x23 0x24 0x54 0x55 0x56 0x80 0x81
 * + DACA(2)+ DACB(2) */
#define AFE_CFG_MAX_WRITES 16

/* idle 期间对电解池的处置。与 main.c 的 IDLE_* 同值。 */
typedef enum {
	AFE_IDLE_STOP_CONV = 0,   /* 只停转换 —— 实测最坏的中间态,仅作对照 */
	AFE_IDLE_KEEP_BIASED = 1, /* 转换不停,持续钳在设定电位 */
	AFE_IDLE_DISCONNECT = 2,  /* 关放大器 ⇒ 真开路,与 CHI660 默认一致 */
} afe_idle_mode_t;

/* 独立输入。派生量一律不放这里(A1)。 */
typedef struct {
	max30131_fsr_t fsr;
	max30131_offset_sel_t off;
	uint8_t conv;      /* CONV_TIME 码 */
	bool conv_pinned;  /* false = 由 auto 派生;true = 用户显式钉住 */
	uint8_t period;    /* SENS_PERIOD 码 */
	uint8_t sysper;    /* SYS_PERIOD 码 */
	bool clk40;        /* CLK_SEL:false=34.952k, true=40.96k */
	uint8_t ioc;       /* IOFFSET_CONV 0..3 */
	bool chop;
	bool rs;
	bool ios;          /* IOS_MODE:1=offset 常在(寄存器图 p96,唯一自洽) */
	int32_t e_mv;      /* E = V_WE − V_RE */
	int32_t vwe_mv;    /* V_WE */
	afe_idle_mode_t idle;
	bool cellv;        /* 电极电位连采 */
	uint8_t satpct;    /* sat 预警余量占零电流码的 % */
	/* 运行态(不由命令直接设,但 plan 要用) */
	bool sensor_selected;
	bool amps_on;
} afe_cfg_t;

/* 全部派生量。审计行 CFG_DERIVED 逐字段打印它。 */
typedef struct {
	int32_t fsr_pa, off_pa, off_min_pa, off_max_pa;
	uint8_t bits;
	uint32_t conv_clk, period_clk;
	int32_t conv_ms, period_ms;
	int32_t idle_ppm;
	int32_t lsb_frame_fa, lsb_eff_fa;
	int16_t rej50_db_x10, rej50_worst_db_x10;
	int conv_alt;              /* auto 的次优码;-1 = 无 */
	int16_t conv_alt_db_x10;
	int32_t red_max_pa, ox_max_pa;
	uint16_t sat_margin;
	int32_t sat_margin_pa;
	int32_t sysbudget_ms, sysper_ms;
	max30131_polarization_t pol;
	/* 警告位:不拒绝,但必然打印(A3) */
	bool idle_warn;       /* idle 窗口 > 10% */
	bool headroom_warn;   /* EOL VDD 下 WEn/REn 越界 */
	bool sig_warn;        /* offset 盖不住已知信号峰值 */
} afe_derived_t;

typedef enum {
	AFE_REJ_NONE = 0,
	AFE_REJ_TOO_LONG,
	AFE_REJ_VERB,
	AFE_REJ_UNKNOWN_KEY,
	AFE_REJ_DUP_KEY,
	AFE_REJ_TOO_MANY_KEYS,
	AFE_REJ_VALUE,
	AFE_REJ_ARG,
	AFE_REJ_PERIOD_LT_CONV,
	AFE_REJ_OFFSET_GT_FSR,
	AFE_REJ_SYSPER_SHORT,
	AFE_REJ_DAC,
	AFE_REJ_DAC_MID,
	AFE_REJ_PERTURB_DURING_RUN,
} afe_rej_code_t;

typedef struct {
	afe_rej_code_t code;
	char key[12];   /* 出错的键名(unknown_key / dup_key / arg 用) */
	int32_t a, b;   /* 与拒因相关的两个数,便于日志自解释 */
} afe_reject_t;

const char *afe_rej_name(afe_rej_code_t code);

typedef struct {
	uint8_t addr;
	uint8_t val;
} afe_write_t;

typedef struct {
	afe_write_t w[AFE_CFG_MAX_WRITES];
	uint8_t n;
	uint8_t skipped;      /* 字节未变而跳过的寄存器数 —— 少一次 SPI 少一次扰动 */
	bool perturbs_cell;   /* 本次变更是否会动电解池 */
} afe_plan_t;

/* 动词 */
typedef enum {
	AFE_VERB_NONE = 0,
	AFE_VERB_START,
	AFE_VERB_STOP,
	AFE_VERB_GET,
	AFE_VERB_STATUS,
	AFE_VERB_SET,
	AFE_VERB_PEEK,
	AFE_VERB_POKE,
	AFE_VERB_OCP,
} afe_verb_t;

typedef struct {
	afe_verb_t verb;
	bool forced;        /* 行尾 FORCE */
	afe_cfg_t cfg;      /* SET/RANGE:打过补丁的候选配置 */
	uint8_t n_keys;
	/* PEEK/POKE/OCP 的裸参数 */
	int32_t arg0, arg1;
} afe_cmd_t;

/*
 * 解析一行命令。`base` 是当前生效配置;未列出的键保持 base 的值(**补丁语义**)。
 * 成功返回 true;失败返回 false 并填 `why`。整行拒绝,绝不部分应用(A2)。
 */
bool afe_cfg_parse(const char *line, const afe_cfg_t *base, afe_cmd_t *out,
		   afe_reject_t *why);

/* 算全部派生量。conv 未钉住时在这里由 auto 策略重算(A1)。 */
void afe_cfg_derive(afe_cfg_t *cfg, afe_derived_t *out);

/*
 * 校验候选配置。`acquiring` 为真时,扰动电解池的键需 `forced` 才放行。
 * 返回 true = 可提交(警告位在 `d` 里,不影响返回值)。
 */
bool afe_cfg_validate(const afe_cfg_t *cfg, const afe_derived_t *d,
		      bool acquiring, bool forced, afe_reject_t *why);

/*
 * 排出写序。🔴 规则「松的先写,紧的后写」——
 * 这条规则可以穷举证明每个中间态都满足 conv ≤ period:
 *   P 变大 ⇒ 先写 0x80(放宽窗口,conv 未变,不变式仍成立)
 *   再写 conv 对(0x23/0x24),按"较短中间态优先"排序
 *   P 变小 ⇒ 最后写 0x80(此时 conv 已是新值 ≤ 新 P)
 * 单测 test_write_order_never_invalid 对全组合枚举每个前缀。
 */
void afe_cfg_plan(const afe_cfg_t *old_cfg, const afe_derived_t *old_d,
		  const afe_cfg_t *new_cfg, const afe_derived_t *new_d,
		  afe_plan_t *out);

/* 审计行格式化器。返回写入长度(不含 NUL);缓冲不足返回 0。 */
size_t afe_cfg_fmt_applied(uint32_t ep, int64_t ms, const char *src,
			   uint8_t nlines, bool forced, const afe_plan_t *plan,
			   const afe_cfg_t *old_cfg, const afe_cfg_t *new_cfg,
			   char *buf, size_t n);
size_t afe_cfg_fmt_derived(uint32_t ep, const afe_cfg_t *cfg,
			   const afe_derived_t *d, char *buf, size_t n);
size_t afe_cfg_fmt_reg(uint32_t ep, uint8_t i, uint8_t total, uint8_t addr,
		       uint8_t before, uint8_t after, uint8_t readback,
		       char *buf, size_t n);
size_t afe_cfg_fmt_reject(uint32_t ep, int64_t ms, const afe_reject_t *why,
			  const char *raw, char *buf, size_t n);

#endif /* AFE_CFG_H */
