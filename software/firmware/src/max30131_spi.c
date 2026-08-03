#include "max30131_spi.h"
#include "max30131_regs.h"

#include <zephyr/drivers/spi.h>
#include <zephyr/logging/log.h>
#include <errno.h>

LOG_MODULE_REGISTER(afe_spi, CONFIG_LOG_DEFAULT_LEVEL);

/*
 * 🔴 帧结构(datasheet Fig.24/25/26)—— 这是本器件最容易写错的一处:
 *
 *     byte0 = A[7:0]  完整 8 位寄存器地址
 *     byte1 = 命令字节,**R/W 在 bit7**(W=0 / R=1),bit[6:0] don't care
 *     byte2.. = 数据
 *
 * 常见错法:`(addr << 1) | rw` —— 那是别家器件的约定,这颗**不是**。
 * 常量 MAX30131_SPI_CMD_WRITE / _READ 来自 lib 的 max30131_regs.h,与单测同源。
 *
 * 时序:SPI mode 0(CPOL=0/CPHA=0),≤8MHz;本板 devicetree 取 4MHz。
 * CS 由 Zephyr 依 cs-gpios(P0.11)自动拉低整帧。
 */

#define AFE_NODE DT_ALIAS(afe0)

/*
 * ⚠️ 2026-07-31 实测:SPI_DT_SPEC_GET 的第三个 delay 参数**已弃用**
 * (Zephyr 4.x:"Delay parameter in SPI DT macros is deprecated, use DT prop instead"),
 * 现在是两参数,CS 时序改由 devicetree 属性给。本器件无特殊 CS 建立/保持要求,
 * 不需要任何 delay 属性。
 */
static const struct spi_dt_spec afe_bus =
	SPI_DT_SPEC_GET(AFE_NODE, SPI_WORD_SET(8) | SPI_TRANSFER_MSB | SPI_OP_MODE_MASTER);

int max30131_spi_init(void)
{
	if (!spi_is_ready_dt(&afe_bus)) {
		LOG_ERR("SPI bus for MAX30131 not ready");
		return -ENODEV;
	}
	return 0;
}

int max30131_spi_write_reg(uint8_t addr, uint8_t val)
{
	uint8_t tx[3] = { addr, MAX30131_SPI_CMD_WRITE, val };
	struct spi_buf txb = { .buf = tx, .len = sizeof(tx) };
	struct spi_buf_set txs = { .buffers = &txb, .count = 1 };

	int rc = spi_write_dt(&afe_bus, &txs);

	if (rc) {
		LOG_ERR("write reg 0x%02x = 0x%02x failed: %d", addr, val, rc);
	}
	return rc;
}

int max30131_spi_read_burst(uint8_t addr, uint8_t *buf, size_t len)
{
	if (buf == NULL || len == 0U) {
		return -EINVAL;
	}

	uint8_t tx[2] = { addr, MAX30131_SPI_CMD_READ };

	/*
	 * 一次 transceive 完成:先发 2 字节(地址+读命令),再收 len 字节。
	 * rx 侧用一个 2 字节的丢弃段对齐发送阶段 —— Zephyr 的 buf_set 是按段
	 * 顺序对齐的,不能只给一个 len 长的 rx buffer,否则会把地址回声读进来。
	 */
	struct spi_buf txb[2] = {
		{ .buf = tx, .len = sizeof(tx) },
		{ .buf = NULL, .len = len }, /* 读阶段发 dummy(NULL = 发 0) */
	};
	struct spi_buf rxb[2] = {
		{ .buf = NULL, .len = sizeof(tx) }, /* 丢弃发送阶段的回声 */
		{ .buf = buf, .len = len },
	};
	struct spi_buf_set txs = { .buffers = txb, .count = 2 };
	struct spi_buf_set rxs = { .buffers = rxb, .count = 2 };

	int rc = spi_transceive_dt(&afe_bus, &txs, &rxs);

	if (rc) {
		LOG_ERR("read burst 0x%02x (%u B) failed: %d", addr, (unsigned)len, rc);
	}
	return rc;
}

int max30131_spi_read_reg(uint8_t addr, uint8_t *val)
{
	return max30131_spi_read_burst(addr, val, 1U);
}
