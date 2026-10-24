# Guided Routes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task.

**Goal:** 三种有限实验模式、有序回访、默认参数复用、顶部全程进度和 ETA。
**Architecture:** 引导文档编译为既有有限动作 / repeat；独立纯进度模块计算估计，控制器提供实际运行快照，原生界面编辑路径并显示。
**Tech Stack:** Python、pytest、Node 假 DOM、原生 JS/CSS。
**Spec:** `docs/superpowers/specs/2026-10-24-guided-routes-v2-design.md`

## Global Constraints

无无限循环、无实物自动试跑、不重置当前 5 秒 / 绑定 / 标定；保留 500 步 / 八小时上限与自然到位无 FE；旧文档和计划哈希兼容。代理仅 GPT-6.1 Sol，共享当前用户修改，不覆盖他人工作，不提交或推送。

## Review Focus

1. 任意首列、折返、同列及轮间返回都先安全抬高。
2. 描述和动作不符、非法 / 禁用 / 超限路径都拒绝，不偷偷排序或裁剪。
3. 多轮同列数据唯一，不能拿上次访问的 ACK / timing 当本次进度。
4. 暂停 / 恢复时 ETA 挂起，停止 / 故障不冒充 100%。
5. 忙时事件和保存均阻断；新编辑撤销旧预览、确认和幂等计划。

### Task 1: 文档与 API（主代理）
Files: `guided_automation.py`、`gui_server.py`、新 `test_guided_routes.py`。
Produces: spec 精确 execution 字段，guided-1.1 到动作 1.1；旧版不变。
- [x] 写旧兼容、三轮、回访、非法 / 禁用 / 超限、参数 / 标定不变及指纹测试，确认红。
- [x] 实现校验、翻译、API 能力及说明；运行针对性测试。

### Task 2: 运行元数据与进度（独立 worker）
Files: `automation.py`、新 `experiment_progress.py`、新进度 / 运行测试。
Consumes: 动作 1.1 可选 execution；Produces: spec 的 progress 与每步 / 样本轮次身份。
- [x] 红测试覆盖执行描述、展开安全返回、唯一数据、真实初始估计、ACK / 暂停 / 故障。
- [x] 最小扩展编译与运行钩子，不修改驱动或 cleanup / STOP 协议。
- [x] 回归 guided runner / STOP 竞态并保存证据。

### Task 3: 界面（独立 worker）
Files: `gui/high_throughput.js`、`gui/styles.css`、新 GUI 测试。
Consumes / Produces: spec 精确 execution / progress，不增加 infinite。
- [x] 模型 / DOM 红测试覆盖解析、默认不变、忙时拒绝、保存及进度状态。
- [x] 三模式、次数 / 路径输入及显式应用、顶部全宽进度 / ETA。
- [x] 相邻 GUI 回归，无写请求查看、失败保存阻断。

### Task 4: 集成与部署（主代理 + 只读审阅）
- [x] 独立审阅 Review Focus、回归修正。
- [x] HTTP 模拟三轮及自定义路径，核对 manifest / 样本。
- [x] 空闲停止确认后备份并更新后台，不伪造旧连接标定。
- [x] 浏览器实见三模式与顶部进度；不实物试跑，记录未验收边界。

结果：专项 112 项通过，全量 2176 通过 / 2 项既有键盘测试失败；证据 `artifacts/guided-routes-20261024/README.md`，不声称全套全绿。
