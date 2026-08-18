# Zephyr developer flash runners. The workstation's normal V4 path instead
# uses its bundled OpenOCD with target-identity checks and page-scoped writes.
# A specific legacy lab probe may still need the V8.80 Commander fallback;
# that compatibility case is not a global toolchain requirement.
include(${ZEPHYR_BASE}/boards/common/nrfjprog.board.cmake)
include(${ZEPHYR_BASE}/boards/common/jlink.board.cmake)
