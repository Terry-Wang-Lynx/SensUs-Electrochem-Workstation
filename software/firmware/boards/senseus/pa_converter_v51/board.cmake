# 烧录器配置。
# 🔴 本机 nrfjprog 未安装,且这支克隆 J-Link 只能配 SEGGER V8.80 —— `west flash`
#    大概率不可用。首烧请用 JLinkExe 手工烧
#    (见 docs/troubleshooting/nrf52-failed-to-power-up-dap.md §0)。
# 🔴 首烧前必须已把 UICR.REGOUT0 烧成 3.3V,否则 VDD=1.8V、SWD 电平对不上,连不上。
# ✅ 首烧之后即可走 USB:MCUboot 上电等 5s 的 CDC ACM serial recovery(见 sysbuild/mcuboot.conf)。
include(${ZEPHYR_BASE}/boards/common/nrfjprog.board.cmake)
include(${ZEPHYR_BASE}/boards/common/jlink.board.cmake)
