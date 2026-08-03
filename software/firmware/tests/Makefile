# Makefile — MAX30131 纯逻辑层单测(开发机直编直跑,不需要 Zephyr/NCS/硬件)
#
# 用途    : make test  编译并运行 lib/max30131 的单元测试
#           make asan  用 AddressSanitizer + UBSan 再跑一遍(抓越界/未定义行为)
#           make clean 清理
# 前置条件: clang 或 gcc(C99)。macOS 自带 Apple clang 即可。
# 快照日期: 2026-07-27

CC      ?= cc
LIB_DIR  = ../lib/max30131
CFLAGS   = -std=c99 -O1 -g \
           -Wall -Wextra -Werror \
           -Wconversion -Wsign-conversion -Wshadow -Wpointer-arith \
           -Wstrict-prototypes -Wmissing-prototypes \
           -I$(LIB_DIR)
SRCS     = test_max30131.c $(LIB_DIR)/max30131.c
BIN      = build/test_max30131
BIN_ASAN = build/test_max30131_asan

.PHONY: test asan all clean

test: $(BIN)
	@./$(BIN)

asan: $(BIN_ASAN)
	@./$(BIN_ASAN)

all: test asan

$(BIN): $(SRCS) $(LIB_DIR)/max30131.h $(LIB_DIR)/max30131_regs.h minitest.h
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $(SRCS)

$(BIN_ASAN): $(SRCS) $(LIB_DIR)/max30131.h $(LIB_DIR)/max30131_regs.h minitest.h
	@mkdir -p build
	$(CC) $(CFLAGS) -fsanitize=address,undefined -fno-omit-frame-pointer \
		-o $@ $(SRCS)

clean:
	rm -rf build
