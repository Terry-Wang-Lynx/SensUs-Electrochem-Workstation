/*
 * MAX30131 SPI 传输层 —— 只管「把字节搬进搬出」,不含任何寄存器语义。
 *
 * 分层理由:所有会算错的东西(寄存器编码、FIFO 解包、counts↔电流、DAC 布局)
 * 都在 lib/max30131/ 里,已用 clang 在 Mac 上单测钉死(139 项断言)。
 * 本文件是薄的、无法在主机上自证的那一层,回板时才插。
 */

#ifndef MAX30131_SPI_H_
#define MAX30131_SPI_H_

#include <stdint.h>
#include <stddef.h>
#include <zephyr/device.h>

/* 取 devicetree 里 alias afe0 指向的 SPI 从设备;失败返回负 errno */
int max30131_spi_init(void);

/* 单寄存器写 */
int max30131_spi_write_reg(uint8_t addr, uint8_t val);

/* 单寄存器读 */
int max30131_spi_read_reg(uint8_t addr, uint8_t *val);

/*
 * 连续读 len 字节(FIFO 与多字节寄存器用)。
 * 🔴 FIFO_DATA(0x0E)读出的是 3 字节/样本的打包格式,解包必须交
 *    max30131_fifo_unpack(),别在这里自己拆位。
 */
int max30131_spi_read_burst(uint8_t addr, uint8_t *buf, size_t len);

#endif /* MAX30131_SPI_H_ */
