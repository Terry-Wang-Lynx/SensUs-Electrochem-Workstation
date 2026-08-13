/*
 * 板级守卫 —— 把 docs/ver4.0/07 §3.5 的「三条焊死项」变成开机就执行的代码,
 * 而不是文档里的一句叮嘱。
 *
 *   1. DCDCEN 必须为 0(DC/DC 电感未贴,置 1 = 连 SWD 一起砖化)
 *   2. POFCON ≈ 2.0V(CR2032 brownout 会落进 POR 盲区、不自恢复)
 *   3. 看门狗必配(同上;唯一的自恢复手段)
 */

#ifndef BOARD_GUARDS_H_
#define BOARD_GUARDS_H_

/*
 * 在任何会启动 USB/AFE 的外设初始化之前调用。V4.0 是只读 no-op;
 * V5.1 会审计 UICR/供电模式并强制关闭未装电感的 REG1 DC/DC。
 * 这里不会启动看门狗,因此可以安全地等待 USB 主机打开 DATA 口。
 */
int board_guards_preflight(void);

/*
 * 传输建立后调用。
 * 返回 0 成功;负 errno 表示看门狗装不上(此时**不要**继续跑采集,
 * 因为失去了 brownout 的唯一自恢复路径)。
 */
int board_guards_init(void);

/* 喂狗。轮询主循环每轮必须调一次。 */
void board_guards_feed(void);

#endif /* BOARD_GUARDS_H_ */
