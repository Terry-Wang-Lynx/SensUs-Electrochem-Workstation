# 烧录器配置。
# 🔴 本机 nrfjprog 未安装,且这支克隆 J-Link 只能配 SEGGER V8.80 —— `west flash`
#    大概率不可用。请改用 07 文档 §0 的 JLinkExe 手工烧录(见 ../../README.md §烧录)。
#    留这行是为了将来装了 nrf-command-line-tools / 换正品探头后能直接用。
include(${ZEPHYR_BASE}/boards/common/nrfjprog.board.cmake)
include(${ZEPHYR_BASE}/boards/common/jlink.board.cmake)
