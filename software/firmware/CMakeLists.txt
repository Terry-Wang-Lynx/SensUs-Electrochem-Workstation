# SPDX-License-Identifier: Apache-2.0
#
# pA-Converter V4.0 / V5.1 共用固件。
# 构建目标决定 RTT 或 USB CDC 传输。
# 说明:  lib/max30131/ 是纯逻辑层,同一份源码既编进固件、也被 tests/ 用 clang
#         在 Mac 上直编直跑(139 项断言)。**不要复制**,只在这里 target_sources 进来。

cmake_minimum_required(VERSION 3.20.0)

# 让 Zephyr 找到本仓库 boards/ 下的自定义板(hwmv2:boards/<vendor>/<board>/)
list(APPEND BOARD_ROOT ${CMAKE_CURRENT_LIST_DIR})
# 让 Zephyr 找到 dts/bindings/ 下的自建 binding 与 vendor 前缀
list(APPEND DTS_ROOT ${CMAKE_CURRENT_LIST_DIR})

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(pa_converter_fw)

option(SENSUS_USE_DEFAULT_MEASUREMENT_CONFIG
  "Build the portable runtime firmware from source-tree defaults" OFF)
if(SENSUS_USE_DEFAULT_MEASUREMENT_CONFIG OR CONFIG_SENSUS_USE_DEFAULT_MEASUREMENT_CONFIG)
	target_compile_definitions(app PRIVATE SENSUS_USE_DEFAULT_MEASUREMENT_CONFIG=1)
endif()

target_sources(app PRIVATE
  src/main.c
  src/max30131_spi.c
  src/board_guards.c
  src/control_transport.c
  lib/max30131/max30131.c        # 纯逻辑层,与单测同源
  lib/max30131/afe_cfg.c         # 命令解析/校验/写序/审计格式化,同源单测
  lib/max30131/measurement_cfg.c # 通用固件的运行时测量条件事务
)

zephyr_include_directories(
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/lib/max30131
)
