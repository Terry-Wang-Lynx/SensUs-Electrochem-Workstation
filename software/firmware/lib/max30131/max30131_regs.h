/*
 * max30131_regs.h — MAX30131 寄存器地址与位域定义(纯常量,零依赖)
 *
 * 用途    : 供 max30131.c(纯逻辑层)与固件 app 层共用的寄存器真值表。
 * 依据    : datasheets-vendor/max30131-max30132-max30134.pdf
 *           - Register Details p81-151(位域/复位值/码表)
 *           - FIFO Description p70-72(Table 6/7/8/9)
 *           - Sensor ADC p39、Offset/Gain Cal p40-41、Sensor DACs p32
 *           设计口径见 docs/ver4.0/05-IC应用设计/U1-MAX30131-AFE应用.md
 * 前置条件: 无(仅 #define)
 * 快照日期: 2026-07-27
 *
 * 🔴 NDA:本文件只含寄存器地址/位偏移这类为实现所必需的事实,不复制 datasheet 正文。
 *         datasheet 本体受 NDA 约束,不外传、不进 web 工具。
 */

#ifndef MAX30131_REGS_H
#define MAX30131_REGS_H

/* ------------------------------------------------------------------ */
/* 状态 / 中断                                                         */
/* ------------------------------------------------------------------ */
#define MAX30131_REG_STATUS1 0x00u
#define MAX30131_STATUS1_A_FULL_Pos 7u
#define MAX30131_STATUS1_FIFO_DATA_RDY_Pos 6u
#define MAX30131_STATUS1_AC_DATA_RDY_Pos 4u
#define MAX30131_STATUS1_EIS_CAL_DONE_Pos 3u
#define MAX30131_STATUS1_INVALID_CFG_Pos 2u
#define MAX30131_STATUS1_VDD_OOR_Pos 1u
/*
 * 🔴 PWR_RDY 的语义与名字相反(datasheet p82 原文):
 *   "PWR_RDY is a read-only bit, and it indicates that VDD **had gone below the
 *    UVLO Threshold (1.55V)**. This bit is not triggered by a soft reset.
 *    This bit is cleared when STATUS 1 (register 0x00) is read or by setting SHDN."
 * ⇒ **1 = 发生过掉压(坏),0 = 正常**。名字读起来像"电源就绪",按字面理解会把
 *   每一次正常开机都报成故障。2026-08-10 首次实测 STATUS1=0x00 时我按字面读错过一次。
 * ⇒ 而且它**读清**:任何读 0x00 的地方都会把它吃掉 ⇒ 必须用 sticky 累积器接。
 */
#define MAX30131_STATUS1_PWR_RDY_Pos 0u

#define MAX30131_REG_INT_ENABLE1 0x05u
#define MAX30131_INT_EN1_A_FULL_Pos 7u
#define MAX30131_INT_EN1_FIFO_DATA_RDY_Pos 6u

/* ------------------------------------------------------------------ */
/* FIFO(Table 6)                                                      */
/* ------------------------------------------------------------------ */
#define MAX30131_REG_FIFO_WR_PTR 0x0Au
#define MAX30131_REG_FIFO_RD_PTR 0x0Bu
#define MAX30131_REG_FIFO_COUNTER1 0x0Cu /* [7]=DATA_COUNT[8], [6:0]=OVF_COUNTER */
#define MAX30131_REG_FIFO_COUNTER2 0x0Du /* DATA_COUNT[7:0] */
#define MAX30131_REG_FIFO_DATA 0x0Eu     /* 突发读 3 字节/样本,地址不自增 */
#define MAX30131_REG_FIFO_CONFIG1 0x0Fu  /* FIFO_A_FULL[7:0] */
#define MAX30131_REG_FIFO_CONFIG2 0x10u

#define MAX30131_FIFO_CONFIG2_FLUSH_Pos 4u
#define MAX30131_FIFO_CONFIG2_STAT_CLR_Pos 3u
#define MAX30131_FIFO_CONFIG2_A_FULL_TYPE_Pos 2u
#define MAX30131_FIFO_CONFIG2_RO_Pos 1u

#define MAX30131_FIFO_DEPTH 256u        /* 256 词 × 21 bit */
#define MAX30131_FIFO_BYTES_PER_WORD 3u /* 每样本必须整 3 字节突发读 */

/* FIFO 词位域(Table 7:byte1[4:0]=F20..F16, byte2=F15..F8, byte3=F7..F0) */
#define MAX30131_FIFO_WORD_MASK 0x1FFFFFu /* 21 bit */
#define MAX30131_FIFO_AUTO_Pos 20u
#define MAX30131_FIFO_TAG4_Pos 16u /* bits[19:16];≤0xC 时 counts=bits[15:0] */
#define MAX30131_FIFO_TAG8_Pos 12u /* bits[19:12];>0xC 时 counts=bits[11:0] */
#define MAX30131_FIFO_TAG4_THRESHOLD 0x0Cu

/* Table 9 中本设计会遇到的 tag */
#define MAX30131_FIFO_TAG_S1_DC 0x0u  /* Sensor 1 DC Current(16-bit counts) */
#define MAX30131_FIFO_TAG_EMPTY 0xFEu /* 读空 FIFO 的哨兵 tag(8-bit tag) */

/* ------------------------------------------------------------------ */
/* 系统 / 基准                                                         */
/* ------------------------------------------------------------------ */
#define MAX30131_REG_SYSTEM_CONTROL 0x14u
#define MAX30131_SYSCTL_RESET_Pos 0u
#define MAX30131_SYSCTL_SHDN_Pos 1u
#define MAX30131_SYSCTL_BYPASS_LDO_Pos 2u
#define MAX30131_SYSCTL_CLK_SEL_Pos 3u /* 0=34.952kHz, 1=40.96kHz */

#define MAX30131_REG_SENSOR_CONFIG 0x1Fu

#define MAX30131_REG_REFERENCE_CONTROL 0x68u
#define MAX30131_REFCTL_REF_EN_Pos 0u
#define MAX30131_REFCTL_REF_VAL_Pos 1u  /* [2:1];00 → 1.536V */
#define MAX30131_REFCTL_REF_MODE_Pos 3u /* 0=内部基准 */

/* ------------------------------------------------------------------ */
/* Sensor 1 AFE / ADC                                                  */
/* ------------------------------------------------------------------ */
#define MAX30131_REG_S1_CONFIG1 0x20u
#define MAX30131_S1C1_CHOP_EN_Pos 0u
#define MAX30131_S1C1_CP_EN_Pos 1u
#define MAX30131_S1C1_CE_DAC_MX_Pos 2u /* [3:2] */
#define MAX30131_S1C1_WE_DAC_MX_Pos 4u /* [5:4] */
#define MAX30131_S1C1_CE_AMP_EN_Pos 6u
#define MAX30131_S1C1_WE_AMP_EN_Pos 7u

#define MAX30131_REG_S1_CONFIG2 0x21u
#define MAX30131_S1C2_SWO_Pos 0u
#define MAX30131_S1C2_RS_Pos 1u
#define MAX30131_S1C2_ILIM_EN_Pos 2u
#define MAX30131_S1C2_SRA_Pos 3u
#define MAX30131_S1C2_SRB_Pos 4u
#define MAX30131_S1C2_SC_Pos 5u
#define MAX30131_S1C2_SWA_Pos 6u
#define MAX30131_S1C2_SWB_Pos 7u

#define MAX30131_REG_S1_CONFIG3 0x22u
#define MAX30131_S1C3_DETECTOR_EN_Pos 0u
#define MAX30131_S1C3_IOS_MODE_Pos 3u /* 1=offset 电流常在(推荐/复位默认) */

#define MAX30131_REG_S1_CONFIG4 0x23u
#define MAX30131_S1C4_OFFSET_SEL_Pos 0u /* [2:0] */
#define MAX30131_S1C4_FSR_Pos 5u        /* [7:5] */

#define MAX30131_REG_S1_CONFIG5 0x24u
#define MAX30131_S1C5_SELECT_Pos 0u
#define MAX30131_S1C5_CONV_TIME_Pos 1u /* [4:1] */

#define MAX30131_REG_S1_IOFFSET_H 0x2Bu /* S1_IOFFSET[15:8] */
#define MAX30131_REG_S1_IOFFSET_L 0x2Cu /* S1_IOFFSET[7:0]  */

/* ------------------------------------------------------------------ */
/* System ADC(12-bit,22 路输入;本设计只用它量 WE1 引脚电压)          */
/* ------------------------------------------------------------------ */
/*
 * 用途:直接数字化 WE1 **引脚**电压,用来回答「不测量时芯片把电极放在哪个电位」——
 * 这个问题从框图推过两次都被实验打脸(见 main.c 的两处 A/B 说明),必须实测。
 * 依据:datasheet System ADC p64-65、寄存器 0x54-0x56 p117-119、FIFO tag Table 9 p72。
 */
#define MAX30131_REG_SYS_ADC_SETUP 0x54u
#define MAX30131_SYSADC_AIN_GAIN_Pos 6u    /* [7:6] */
#define MAX30131_SYSADC_PWR_GAIN_Pos 4u    /* [5:4] */
#define MAX30131_SYSADC_SENSV_GAIN_Pos 2u  /* [3:2] 管 WOn/WEn/REn/CEn */
/*
 * 🔴 OPA_BYPASS_EN 必须保持 0。置 1 会旁路输入缓冲,datasheet 明说此时
 * 「the signal must be able to drive a 14MΩ load」⇒ 在 WE 上等于挂了条
 * 0.4V/14MΩ ≈ 29nA 的漏电路径,足以把我们要测的东西本身破坏掉。
 * 缓冲使能时「the input signal loading is negligible」。
 */
#define MAX30131_SYSADC_OPA_BYPASS_EN_Pos 1u
/* 增益码:00=2.0 / 01=1.0 / 10=0.5 / 11=0.25(V/V) */
#define MAX30131_SYSADC_GAIN_2X 0x0u
#define MAX30131_SYSADC_GAIN_1X 0x1u
#define MAX30131_SYSADC_GAIN_0P5X 0x2u
#define MAX30131_SYSADC_GAIN_0P25X 0x3u

#define MAX30131_REG_SYS_ADC_IN_SEL1 0x55u
/*
 * ⚠️ SYS_SELECT 的位号未能从 datasheet 文本层确认(表格被提取工具打散)。
 * 按本 datasheet 的排版惯例(字段说明的最后一项 = LSB,与 0x56 的
 * S1_CE_SYS_SEL=bit0 一致)取 bit0。假设若错,后果只是拿不到 0xD1 数据 ——
 * 可检测、无损;固件会在 bring-up 时明确报出来,不会静默。
 */
#define MAX30131_SYSADC_SYS_SELECT_Pos 0u

#define MAX30131_REG_SYS_ADC_IN_SEL2 0x56u
#define MAX30131_SYSADC_S1_CE_SEL_Pos 0u
#define MAX30131_SYSADC_S1_RE_SEL_Pos 1u
#define MAX30131_SYSADC_S1_WE_SEL_Pos 2u /* ← 我们要的那一位 */
#define MAX30131_SYSADC_S1_WO_SEL_Pos 3u

/* FIFO tag(Table 9,8-bit tag 分支,数据在 bits[11:0]) */
#define MAX30131_FIFO_TAG_S1_WO_V 0xD0u
#define MAX30131_FIFO_TAG_S1_WE_V 0xD1u
#define MAX30131_FIFO_TAG_S1_RE_V 0xD2u
#define MAX30131_FIFO_TAG_S1_CE_V 0xD3u

/* ------------------------------------------------------------------ */
/* 极化 DAC(单通道版只有 DACA/DACB)                                    */
/* ------------------------------------------------------------------ */
/*
 * 🔴 DAC 是「MSB 字节 + EN/LSB 字节」的非直觉布局(datasheet 寄存器总表 p79):
 *   0x69 DACA MSB     = DACA_CODE[11:4]           (整字节)
 *   0x6A DACA EN LSB  = DACA_CODE[3:0]<<4 | 保留[3:1] | DACA_EN(bit0)
 * 即 code = (MSB<<4) | (ENLSB>>4);EN 在 **LSB 字节的 bit0**,不是 MSB 的 bit7。
 */
#define MAX30131_REG_DACA_MSB 0x69u
#define MAX30131_REG_DACA_ENLSB 0x6Au
#define MAX30131_REG_DACB_MSB 0x6Bu
#define MAX30131_REG_DACB_ENLSB 0x6Cu
#define MAX30131_DAC_ENLSB_CODE_Pos 4u /* CODE[3:0] 在 bit[7:4] */
#define MAX30131_DAC_ENLSB_EN_Pos 0u
#define MAX30131_DAC_BITS 12u
#define MAX30131_DAC_FULL_SCALE 4096u
#define MAX30131_DAC_CODE_MAX 4095u
/* datasheet p32:VDACx = VREF·CODE/2^12 仅对 CODE > 18 LSB 成立 */
#define MAX30131_DAC_CODE_LINEAR_MIN 19u

/* DAC mux 码(S1_CONFIG1 的 WE_DAC_MX / CE_DAC_MX) */
#define MAX30131_DAC_MX_A 0u
#define MAX30131_DAC_MX_B 1u
#define MAX30131_DAC_MX_C 2u
#define MAX30131_DAC_MX_D 3u

/* ------------------------------------------------------------------ */
/* 转换控制                                                            */
/* ------------------------------------------------------------------ */
#define MAX30131_REG_CONVERT_SETUP1 0x80u

/*
 * 0x81 CONVERT SETUP2:TEMP_PERIOD[7:4] + SYS_PERIOD[3:0](p142)。
 * 码表与 SENS_PERIOD 同一张:0x0=124ms / 0x1=242 / 0x2=476 / 0x3=945 / 0x4=1882 …
 * TEMP_SELECT(0x60)本设计不开,所以 TEMP_PERIOD 取什么都无所谓,留 0。
 */
#define MAX30131_REG_CONVERT_SETUP2 0x81u
#define MAX30131_CS2_SYS_PERIOD_Pos 0u  /* [3:0] */
#define MAX30131_CS2_TEMP_PERIOD_Pos 4u /* [7:4] */
#define MAX30131_CS1_SENS_PERIOD_Pos 0u   /* [3:0] */
#define MAX30131_CS1_SYS_CONV_TYPE_Pos 4u
#define MAX30131_CS1_IOFFSET_CONV_Pos 5u  /* [6:5] */
#define MAX30131_CS1_SENS_CONV_TYPE_Pos 7u /* 0=DC, 1=EIS */

#define MAX30131_REG_CONVERT_START 0x83u
#define MAX30131_CSTART_CONVERT_Pos 0u
#define MAX30131_CSTART_AUTO_Pos 1u

/*
 * INTB SETUP(p149):bit2 = EN_VDD_OOR,bit[1:0] = INTB_OCFG。**复位值 EN_VDD_OOR=0**。
 * 🔴 p82:"To enable VDD_OOR functionality, both EN_VDD_OOR (0x95) and REF_EN (0x68)
 *   must be set to 1 (enabled); **otherwise, VDD_OOR is always set to 0**."
 * ⇒ 不写这个寄存器,STATUS1.VDD_OOR 恒为 0 —— 那个"掉压监测"是**死的**。
 *   本项目 2026-08-10 之前一直没写它,所以之前所有"VDD_OOR=0"都不构成证据。
 * INTB_OCFG 取 0x0(open-drain)—— 本板 INTB 悬空,选开漏保证它永不驱动。
 */
#define MAX30131_REG_INTB_SETUP 0x95u
#define MAX30131_INTB_EN_VDD_OOR_Pos 2u
#define MAX30131_INTB_OCFG_Pos 0u
#define MAX30131_INTB_OCFG_OPEN_DR 0u

/* ------------------------------------------------------------------ */
/* ID                                                                  */
/* ------------------------------------------------------------------ */
#define MAX30131_REG_REV_ID 0xFEu
#define MAX30131_REG_PART_ID 0xFFu

/* ------------------------------------------------------------------ */
/* SPI 帧(4 线,≤8MHz;datasheet p68-70 Fig.24/25/26)                  */
/* ------------------------------------------------------------------ */
/*
 * 🔴 帧结构是 [地址字节][命令字节][数据字节…],共 24 clk 起:
 *   byte0 = A[7:0] 完整 8 位寄存器地址
 *   byte1 = 命令字节,**R/W 在 bit7**(W=0 / R=1),bit[6:0] don't care
 *   byte2 = 数据
 * 常见错法:把地址左移一位再拼 R/W(那是别家器件的约定,这颗不是)。
 *
 * 时序:数据在 SCLK 上升沿打入器件、下降沿打出 → SPI mode 0(CPOL=0/CPHA=0)。
 * 突发:普通寄存器每多 8 clk 地址自增;**FIFO_DATA 地址不自增**,自增的是
 *       FIFO_RD_PTR,且每样本必须整 3 字节(见 MAX30131_FIFO_BYTES_PER_WORD)。
 *       FIFO 读事务最少 5 字节 = 地址 + 命令 + 3 数据。
 * ⚠️ datasheet 在 p70 写 FIFO "22-bit wide",而 p70 FIFO Description 与 Table 7
 *    写 21-bit。按 21 bit 取(MAX30131_FIFO_WORD_MASK),多余高位当保留位屏蔽。
 */
#define MAX30131_SPI_MAX_HZ 8000000u
#define MAX30131_SPI_FRAME_MIN_BYTES 3u
#define MAX30131_SPI_CMD_WRITE 0x00u
#define MAX30131_SPI_CMD_READ 0x80u

#endif /* MAX30131_REGS_H */
