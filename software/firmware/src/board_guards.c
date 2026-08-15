#include "board_guards.h"

#include <zephyr/kernel.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/logging/log.h>
#include <nrfx.h>
#include <hal/nrf_power.h>
#include <errno.h>

LOG_MODULE_REGISTER(guards, CONFIG_LOG_DEFAULT_LEVEL);

/*
 * 喂狗窗口。轮询周期由 SENS_PERIOD 定(0x5 档 ≈3.757s),再加 FIFO 批量等待,
 * 最坏一轮可能几十秒。取 60s 上限,给足余量又不至于让真死机拖太久。
 */
#define WDT_WINDOW_MAX_MS 60000U

static const struct device *wdt_dev;
static int wdt_ch = -1;

#if defined(CONFIG_BOARD_PA_CONVERTER_V51)
static uint32_t boot_uicr_approtect;
static uint32_t boot_uicr_regout0;
static nrf_power_mainregstatus_t boot_mainregstatus;
#endif

int board_guards_preflight(void)
{
#if defined(CONFIG_BOARD_PA_CONVERTER_V51)
	bool bad = false;

	/* Read only: application firmware never writes UICR. */
	boot_uicr_approtect = NRF_UICR->APPROTECT;
	boot_uicr_regout0 = NRF_UICR->REGOUT0;
	boot_mainregstatus = nrf_power_mainregstatus_get(NRF_POWER);

	if (nrf_power_dcdcen_get(NRF_POWER)) {
		nrf_power_dcdcen_set(NRF_POWER, false);
		bad = true;
	}
	if ((boot_uicr_approtect & UICR_APPROTECT_PALL_Msk) !=
	    UICR_APPROTECT_PALL_HwDisabled) {
		bad = true;
	}
	if ((boot_uicr_regout0 & UICR_REGOUT0_VOUT_Msk) !=
	    UICR_REGOUT0_VOUT_3V3) {
		bad = true;
	}
	if (boot_mainregstatus != NRF_POWER_MAINREGSTATUS_HIGH) {
		bad = true;
	}

	return bad ? -EPERM : 0;
#else
	return 0;
#endif
}

/* ------------------------------------------------------------------ */
/* [1] DCDCEN 断言                                                     */
/* ------------------------------------------------------------------ */
/*
 * 🔴 本板 DC/DC 的 LC(L4 15nH + L3 10µH + C14 47nF)**未贴**。
 * 若 DCDCEN=1,芯片会因为没有电感而彻底不工作 —— 连 SWD 都上不来,
 * 也就是说**一旦烧进去就再也连不上、只能靠 recover 抢**。
 *
 * defconfig 里已 CONFIG_BOARD_ENABLE_DCDC=n;这里在运行时再确认一次,
 * 防止将来有人加了 overlay / prj.conf 覆盖把它打开。
 * 发现被打开就**立刻关掉**并大声报错 —— 关掉比继续跑安全。
 */
static void assert_dcdc_disabled(void)
{
	if (nrf_power_dcdcen_get(NRF_POWER)) {
		LOG_ERR("🔴 DCDCEN=1 但本板未贴 DC/DC 电感 —— 立即关闭。"
			"检查是否有 overlay/prj.conf 打开了 CONFIG_BOARD_ENABLE_DCDC");
		nrf_power_dcdcen_set(NRF_POWER, false);
	} else {
#if defined(CONFIG_BOARD_PA_CONVERTER_V51)
		LOG_INF("DCDCEN=0 OK(LDO mode)");
#else
		LOG_INF("DCDCEN=0 OK(LDO 模式,radio 电流约 2x,续航按此预算)");
#endif
	}
}

/* ------------------------------------------------------------------ */
/* [2] POFCON ≈ 2.0V                                                   */
/* ------------------------------------------------------------------ */
/*
 * 🔴 为什么必须配:CR2032 内阻随放电与低温上升,BLE/采样瞬时电流会把轨拉出
 * 一个凹陷。若凹陷落进「低于工作电压但高于 POR 复位阈值」的盲区,
 * 芯片会停在未定义状态而**不复位、不自恢复**。POF 在 2.0V 就主动告警/复位,
 * 把盲区堵掉。
 *
 * 本板 VDD = VDDH = +BATT(normal voltage mode),所以用 THRESHOLD 而不是
 * THRESHOLDVDDH。2.0V 与 05 文档的 EOL 口径一致(CR2032 到 2.0V 判 EOL)。
 */
static void setup_power_fail_comparator(void)
{
	nrf_power_pofcon_set(NRF_POWER, true, NRF_POWER_POFTHR_V20);
#if defined(CONFIG_BOARD_PA_CONVERTER_V51)
	nrf_power_pofcon_vddh_set(NRF_POWER, NRF_POWER_POFTHRVDDH_V42);
	LOG_INF("POFCON = ON @2.0V VDD / 4.2V VDDH(USB high-voltage mode)");
#else
	LOG_INF("POFCON = ON @2.0V(VDD=VDDH normal voltage mode)");
	#endif
}

/* ------------------------------------------------------------------ */
/* [3] 看门狗                                                          */
/* ------------------------------------------------------------------ */
static int setup_watchdog(void)
{
	wdt_dev = DEVICE_DT_GET(DT_NODELABEL(wdt0));

	if (!device_is_ready(wdt_dev)) {
		LOG_ERR("watchdog device not ready");
		return -ENODEV;
	}

	struct wdt_timeout_cfg cfg = {
		.window = { .min = 0U, .max = WDT_WINDOW_MAX_MS },
		.callback = NULL, /* 直接复位,不做 pre-reset 回调 */
		.flags = WDT_FLAG_RESET_SOC,
	};

	wdt_ch = wdt_install_timeout(wdt_dev, &cfg);
	if (wdt_ch < 0) {
		LOG_ERR("wdt_install_timeout failed: %d", wdt_ch);
		return wdt_ch;
	}

	/*
	 * 🔴 WDT_OPT_PAUSE_HALTED_BY_DBG 必须给 —— 否则调试器一 halt 就被狗咬,
	 * 单步/断点全都没法用。nRF52 的 WDT 一旦 start 就**无法停止**(除复位),
	 * 所以这个选项是调试期的唯一出路。
	 */
	int rc = wdt_setup(wdt_dev, WDT_OPT_PAUSE_HALTED_BY_DBG);

	if (rc) {
		LOG_ERR("wdt_setup failed: %d", rc);
		return rc;
	}

	LOG_INF("watchdog armed, window %u ms (paused while halted by debugger)",
		WDT_WINDOW_MAX_MS);
	return 0;
}

/* ------------------------------------------------------------------ */
int board_guards_init(void)
{
	int rc = board_guards_preflight();

	if (rc != 0) {
#if defined(CONFIG_BOARD_PA_CONVERTER_V51)
		LOG_ERR("V5.1 power/UICR audit failed: APPROTECT=0x%08x "
			"REGOUT0=0x%08x MAINREGSTATUS=%u (required: 0x5a, 3.3V, VDDH)",
			boot_uicr_approtect, boot_uicr_regout0,
			(unsigned)boot_mainregstatus);
#else
		LOG_ERR("board preflight failed: %d", rc);
	#endif
		return rc;
	}

#if defined(CONFIG_BOARD_PA_CONVERTER_V51)
	LOG_INF("V5.1 UICR/power audit OK: APPROTECT=0x%02x REGOUT0=3.3V VDDH=active",
		(unsigned)(boot_uicr_approtect & UICR_APPROTECT_PALL_Msk));
#endif

	assert_dcdc_disabled();
	setup_power_fail_comparator();

	rc = setup_watchdog();

	if (rc) {
		/*
		 * 看门狗装不上就不要继续采集:brownout 落进 POR 盲区时
		 * 没有任何自恢复手段,现场会变成"设备静默"而非"设备重启",
		 * 排查代价高得多。
		 */
		LOG_ERR("🔴 看门狗未就绪 —— 拒绝进入采集(brownout 无自恢复路径)");
	}
	return rc;
}

void board_guards_feed(void)
{
	if (wdt_dev != NULL && wdt_ch >= 0) {
		(void)wdt_feed(wdt_dev, wdt_ch);
	}
}
