# pA-Converter V5.1 board support

This board definition is derived from the version-controlled V5.1 KiCad PCB,
not from a generic Nordic development kit. The MAX30131 SPI mapping is the
same as V4.0 (`MISO=P0.04`, `SCLK=P0.05`, `MOSI=P1.09`, `CSN=P0.11`), and
`INTB` remains unconnected. The V4.0 and V5.1 targets therefore compile the
same electrochemistry method, register configuration, audit, and sample
conversion sources. Only the board, boot, and host transport layers differ.

V5.1 exposes two USB CDC ACM interfaces: SMP reset/update traffic and the
existing text measurement/control protocol. Host software identifies the board
from its USB descriptor and verifies the DATA status stream; it must not rely
only on macOS device suffixes.

The flash map reproduces the verified handoff layout: MCUboot starts at
`0x00000`, slot 0 at `0x18000`, and the two image slots are equal sized.

Safety constraints:

- A blank nRF52833 has no suitable factory USB bootloader. Initial bootstrap
  requires SWD; routine signed application updates then use MCUboot over USB.
- UICR `REGOUT0.VOUT` must already be `3.3 V` (`5`) before this application
  can run safely.
- UICR `APPROTECT.PALL` must be hardware-disabled (`0x5a`) so SWD recovery is
  retained during bring-up.
- Never issue a whole-chip erase during routine updates: it can erase these
  UICR settings. The firmware only audits UICR and never writes it.
- The REG1 DC/DC regulator must stay disabled because its inductor is not
  fitted. The nRF52833 high-voltage REG0 path is selected by VDDH and UICR.
- The current PCB routes USB-C CC/data for only one plug orientation. If the
  board does not enumerate, rotate the USB-C plug before diagnosing firmware.
- MCUboot waits five seconds for serial recovery. The application can reset
  into that window over its SMP CDC interface, so normal App updates do not
  require a manual unplug/replug.

Bench evidence:

- 2026-08-12: SWD bootstrap, MCUboot USB enumeration, SMP upload, image boot,
  and debugger-independent recovery were verified on hardware.
- 2026-08-13: the handed-off MCUboot accepted the signed dual-CDC application;
  macOS exposed SMP and DATA separately, and a 180 s PBS I-T run completed
  1452/1452 native samples without a sequence gap, FIFO overflow, current
  saturation, invalid configuration, VDD fault, or brownout.
- 2026-08-15: the App detected the same V5.1 unit by its live status stream,
  applied a 150 s profile over SMP, and read the resulting AFE configuration
  back from DATA. Analog CE/WO headroom still requires per-run audit and is not
  implied by successful USB communication.
