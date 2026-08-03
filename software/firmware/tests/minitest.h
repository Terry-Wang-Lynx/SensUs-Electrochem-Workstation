/*
 * minitest.h — 极小断言框架(零依赖,单头文件)
 *
 * 用途    : 让 lib/ 下的纯逻辑层能在开发机上用 clang/gcc 直接编译运行单测,
 *           不需要 Zephyr、不需要 native_sim(后者非 macOS 官方支持目标)、
 *           不需要装 NCS 工具链。
 * 用法    : #include "minitest.h";用 TEST(name){...} 定义,RUN(name) 注册;
 *           main 里 return mt_report();
 * 前置条件: C99。
 * 快照日期: 2026-07-27
 */

#ifndef MINITEST_H
#define MINITEST_H

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

static int mt_checks;
static int mt_fails;
static const char *mt_current;

#define TEST(name) static void name(void)

#define RUN(name)                                                              \
	do {                                                                   \
		mt_current = #name;                                            \
		name();                                                        \
	} while (0)

#define MT_FAIL(fmt, ...)                                                      \
	do {                                                                   \
		mt_fails++;                                                    \
		printf("  ✗ [%s] %s:%d  " fmt "\n", mt_current, __FILE__,      \
		       __LINE__, __VA_ARGS__);                                 \
	} while (0)

/* 整数相等(带十进制+十六进制显示,寄存器断言看得清) */
#define CHECK_EQ(actual, expected)                                             \
	do {                                                                   \
		long long a_ = (long long)(actual);                            \
		long long e_ = (long long)(expected);                          \
		mt_checks++;                                                   \
		if (a_ != e_) {                                                \
			MT_FAIL("%s: got %lld (0x%llX), want %lld (0x%llX)",   \
				#actual, a_, (unsigned long long)a_, e_,       \
				(unsigned long long)e_);                       \
		}                                                              \
	} while (0)

/* 允许 ±tol 的相等(用于四舍五入/量化) */
#define CHECK_NEAR(actual, expected, tol)                                      \
	do {                                                                   \
		long long a_ = (long long)(actual);                            \
		long long e_ = (long long)(expected);                          \
		long long t_ = (long long)(tol);                               \
		long long d_ = a_ > e_ ? a_ - e_ : e_ - a_;                    \
		mt_checks++;                                                   \
		if (d_ > t_) {                                                 \
			MT_FAIL("%s: got %lld, want %lld ±%lld (diff %lld)",   \
				#actual, a_, e_, t_, d_);                      \
		}                                                              \
	} while (0)

#define CHECK_TRUE(cond)                                                       \
	do {                                                                   \
		mt_checks++;                                                   \
		if (!(cond)) {                                                 \
			MT_FAIL("%s: expected true", #cond);                   \
		}                                                              \
	} while (0)

#define CHECK_FALSE(cond)                                                      \
	do {                                                                   \
		mt_checks++;                                                   \
		if (cond) {                                                    \
			MT_FAIL("%s: expected false", #cond);                  \
		}                                                              \
	} while (0)

static int mt_report(void)
{
	if (mt_fails == 0) {
		printf("\n✅ 全部通过:%d 项断言\n", mt_checks);
		return 0;
	}
	printf("\n❌ %d/%d 项断言失败\n", mt_fails, mt_checks);
	return 1;
}

#endif /* MINITEST_H */
