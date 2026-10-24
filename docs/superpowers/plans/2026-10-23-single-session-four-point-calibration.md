# 单次上电四点标定 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Do not commit, branch, push or touch physical hardware without root coordination.

**Goal:** 普通高通量页只保留一次标定：物理第1列X、第12列X、独立浸入Z、提出安全Z；带电松轴手拧、只读记录、受门禁的原位锁轴，不再要求另读机械四极限。

**Architecture:** GuidedAutomation保管当前连接四点、完整profile/参考身份；断电/重连不沿用旧计数。原三点历史记录可以读取查看/模拟，不授权新实物运动。生产普通模板实物运动使用本次四点，不再依赖独立绝对限位；旧底层限位API仅兼容，不能从客户端JSON绕过新的可信标定入口。原位锁轴没有手册保证，故只在两个目标位置寄存器均接近当前实读位置时才允许显式finish使能；否则在F3使能前阻断，不发位置/回零/清零指令。

**Tech Stack:** existing Python/pyserial, local JS/Three.js, pytest/Node contract tests.

## Global Constraints

- User确认Z已支撑或不会下滑，授权两轴松轴；根代理已一次F3失能两轴，0RPM/status02，保持供电，未重新使能或发位移。
- 松轴实测后FE STOP返回E2（失能条件不满足），不能伪造ACK；修复应仅将明确E2且两次健康失能/静止反馈识别为“已失能”，仍保留ACK停止与失能停止的证据差异。任何已使能/未知反馈/错误码不放行。
- 不修改电机非易失配置、硬件/固件；不清零/回零、不自动返回，不混合不同上电周期的点。
- 如重新锁轴目标检查不满足，不猜测控制器内部行为，不发送试探性位置命令。

## Tasks

1. Root owns emm_motion.py / driver tests: fix disabled-stop close root cause through red→green; add supported powered manual capture and strict no-jump target-read relock gate, actual motion stays explicit/guarded. No blind enabling.
2. Guided model worker owns guided_automation.py and its unit/runner tests: four independent captures first/last/working/safe, legacy viewing compatibility, token/profile identity, working Z no longer inferred from first X capture. Begin requires release/support confirmations for production; legacy qualified test drivers remain usable. Expose incomplete/qualification status honestly.
3. UI worker owns high_throughput.js/styles.css and guided GUI tests: one compact calibration section with four read buttons and clear keep-powered/support warnings. Remove old absolute boundary/datum setup from ordinary UI (legacy hidden elements may remain if needed by compatibility code), no eight-point or numeric-CAD requirement. Finish explicit locking, honest blocked messages. State distinguishes released axes, linked vs ready.
4. Root owns server/production policy/integration tests: literal release/Z-support confirmations, no required independent limits for trusted guided runs; reject arbitrary raw live-motion programs without trusted same-session four-point calibration. Existing ordinary no-motion acquisition and simulate preserved. If new manual flow needs target check, no raw command injection.
5. Independent review, serial full regression, JS checks, browser, source packages. Root alone physical checks; currently motors intentionally released. No automatic relock or user hand move during backend update.

## Review Focus

- Power/connection change between any two captures or before finish/run rejects old baseline.
- Zworking independent of Xfirst; first/last always physical1/12 regardless selected columns.
- Missing targets, target mismatch, enable rejection, unknown STOP cannot be called successful relock.
- Removing extra absolute-limit UI does not allow raw JSON/client flags to authorize arbitrary motion.
- UI says released/locked from actual motor feedback, not just a serial port or saved template.
