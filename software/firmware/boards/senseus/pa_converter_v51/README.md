# pA-Converter V5.1 board support

This board definition is derived from the version-controlled V5.1 KiCad PCB,
not from a generic Nordic development kit.  The MAX30131 SPI mapping is the
same as V4.0 (`MISO=P0.04`, `SCLK=P0.05`, `MOSI=P1.09`, `CSN=P0.11`), and
`INTB` remains unconnected.  Two USB CDC ACM interfaces separate SMP recovery
traffic from the existing text measurement/control protocol.

The flash map reproduces the handed-off image layout: MCUboot starts at
`0x00000` and slot 0 at `0x18000`.  The two image slots are equal sized.

Safety constraints:

- UICR `REGOUT0.VOUT` must already be `3.3 V` (`5`) before this application
  can run safely.
- UICR `APPROTECT.PALL` must be hardware-disabled (`0x5a`) so SWD recovery is
  retained during bring-up.
- The REG1 DC/DC regulator must stay disabled because its inductor is not
  fitted.  nRF52833's high-voltage REG0 path has no separately controlled
  DC/DC stage; it is selected by the VDDH supply and UICR output setting.
- The firmware never writes UICR; it only audits these values at startup.

Bench validation (2026-08-13): the handed-off MCUboot accepted the signed app
over its single CDC recovery interface, after which the app enumerated two CDC
interfaces.  On the tested macOS host interface 0 appeared as
`/dev/cu.usbmodem1101` (SMP) and interface 2 as `...1103` (DATA); applications
must identify the SMP endpoint with an `os echo` probe or explicit configuration
instead of relying on those host-specific suffixes.  A 180 s PBS I-T run on DATA
completed 1452/1452 native samples with no sequence gap, FIFO overflow,
saturation, invalid configuration, VDD fault, or brownout.
