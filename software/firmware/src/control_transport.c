#include "control_transport.h"

#include <zephyr/kernel.h>
#include <zephyr/sys/util.h>

#if defined(CONFIG_BOARD_PA_CONVERTER_V51)

#include <nrfx.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/usb/usb_device.h>
#include <errno.h>

BUILD_ASSERT(DT_NODE_HAS_COMPAT(DT_CHOSEN(zephyr_console), zephyr_cdc_acm_uart),
	     "V5.1 DATA transport must be a CDC ACM UART");

static const struct device *const data_cdc =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

int control_transport_init(void)
{
	uint32_t dtr = 0U;
	int rc;

	if (!device_is_ready(data_cdc)) {
		return -ENODEV;
	}

	/*
	 * V5.1 fits the small HFXO covered by the 1024 us recommendation in
	 * the nRF52833 product specification.  USB starts the HFXO, so this
	 * register must be set before usb_enable().
	 */
	NRF_CLOCK->HFXODEBOUNCE = CLOCK_HFXODEBOUNCE_HFXODEBOUNCE_Db1024us;

	rc = usb_enable(NULL);
	if (rc != 0) {
		return rc;
	}

	/* Do not arm the watchdog until the host has opened the DATA port. */
	while (dtr == 0U) {
		rc = uart_line_ctrl_get(data_cdc, UART_LINE_CTRL_DTR, &dtr);
		if (rc != 0) {
			return rc;
		}
		k_sleep(K_MSEC(100));
	}

	return 0;
}

int control_transport_read_char(char *ch)
{
	unsigned char byte;
	int rc;

	if (ch == NULL) {
		return -EINVAL;
	}

	rc = uart_poll_in(data_cdc, &byte);
	if (rc == 0) {
		*ch = (char)byte;
		return 1;
	}
	if (rc == -1) {
		return 0;
	}
	return rc;
}

#else

#include <SEGGER_RTT.h>
#include <errno.h>

int control_transport_init(void)
{
	return 0;
}

int control_transport_read_char(char *ch)
{
	if (ch == NULL) {
		return -EINVAL;
	}
	return SEGGER_RTT_Read(0U, ch, 1U) == 1U ? 1 : 0;
}

#endif
