# Initial bootstrap still requires SWD; routine application updates use the
# board's MCUboot USB serial-recovery path.
include(${ZEPHYR_BASE}/boards/common/nrfjprog.board.cmake)
include(${ZEPHYR_BASE}/boards/common/jlink.board.cmake)
