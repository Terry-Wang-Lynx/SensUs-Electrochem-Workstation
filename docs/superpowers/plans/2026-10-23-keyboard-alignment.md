# Keyboard Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Use apply_patch/TDD; do not commit/branch/push. Root alone operates hardware/browser.

**Goal:** 每次启动/重新连接后显式进入四点键盘标定，WASD/方向键仅此模式可用，替代手拧与松轴；速度可调且有服务器硬上限、松键/失焦/失联停止。

**Architecture:** 新键盘标定保持两轴锁定，使用有最大位移的相对FD短段，不使用失联后无限持续的F6。独立Jog lease worker以单个在途段运行，客户端持续心跳续约；GUI keyup/blur/hidden/tab/pagehide发送停止，后台单调时钟过期后停止，迟到请求不能复活。四点复用现有严格式本连接/profile身份与实验范围，完成不再松轴/重新锁轴。旧手拧接口仅历史兼容。

**Tech Stack:** Existing Python/pyserial, local JS, pytest/Node FakeSerial. Keep current dirty working checkout because the live local app and all prior user work use it; no new branch or worktree without user request.

## Constraints and rulings

- Defaults: speed0.5mm/s, operator ceiling1.0mm/s; server hard ceiling2.0mm/s and configured axis maximum; no below-one-RPM speed rounding upward. Millimeters per second is target command speed, actual displacement is encoder feedback and short-press timing includes communication/acceleration.
- Only one axis/direction at a time. A/left=Xleft/CW0, D/right=Xright/CCW1, W/up=Zup/CCW1, S/down=Zdown/CW0. Opposing/multiple direction keys stop; require keys released before a new hold. Ignore OS key repeat.
- Each FD segment ≤0.1mm and ≤0.12s nominal duration, no queue/pipelining. Lease350ms, UI heartbeats100ms. Max continuous hold30s and10mm; reaching cap stops and requires release/repress. Lost serial still cannot guarantee hardware stop; finite segment limits committed position, not safety-rated hard limits.
- Initial enable only after fresh full profiles, stationary healthy axes, both33/34 target registers near current counts. No hardwarezero/home or target-reset guesses. Current old session was released and hand-turned; if targets differ, keyboard begin must fail before F3 and instruct one fresh power-up, then no hand turning. Root will NOT automatically enable or jog the real rig this task.
- Every explicit begin invalidates prior four-point entitlement. New process/connection cannot restore old authorization. Legacy interrupted/released sessions may be explicitly superseded ONLY after fresh safe STOP, profiles and target check; never clear unknown output recovery by a client flag.
- Key capture only when HT view+calibration UI active, keyboard session valid, focused window, not input/select/textarea/contenteditable/IME, no Ctrl/Alt/Meta. Escape stops. Finish/cancel/capture/tab exit freeze and drain jog before touching points/session, no late heartbeat/start revival.
- STOP retains truthful ACK vs disabled-feedback evidence; unknown STOP blocks everything except explicit recovery. Do not remove existing automatic experiment range/profile checks.

## Interfaces

Driver (worker1): begin_keyboard_alignment(confirmed=False), alignment_step(axis,direction,speed_mm_s,duration_s,max_speed_mm_s,cancel_event=None), stop_keyboard_alignment(), capture_keyboard_alignment(), finish_keyboard_alignment(reference_counts), cancel_keyboard_alignment(). All counter/profile/session identity trusted, no client raw counters. Stop method preserves a healthy alignment generation only for owned jog stop; global stop invalidates session.

Domain (worker2): new alignment_jog.py manager; GuidedAutomation.begin_keyboard_calibration(motion,confirmed=False), alignment_snapshot(), start_jog(payload), heartbeat_jog(payload), stop_jog(payload), configure_jog(payload), freeze_alignment()/resume_alignment(). Existing capture/finish/cancel branch by manual.control_mode='keyboard'. Snapshot.manual has control_mode/session_token and alignment status/settings. Root endpoint names under guided/calibration/jog/{start,heartbeat,stop,config}; begins via existing guided/calibration/begin with {control_mode:'keyboard',motion_confirmed:true}. Workers coordinate exact payloads and snapshots directly before implementation.

UI(worker3): ordinary calibration is keyboard only; explicit start+motion-clearance checkbox (no release/Z-support checkbox shown). Four existing capture ids retained. Speed and max fields, press-and-hold arrow pad plusWASD. Stop-before-response race handled using client-created press_id and per-session token; canceled ids remembered until session end (hard cap rejects further commands, no eviction revival). Use frontend heartbeat sequence, stop unknown in-flight press ids, never delayed start after keyup.

Root: server route guards, priority stop/global stop/shutdown hooks, production policy/audit/packaging/docs, integration tests. No joins/driver worker stop while holding controller lock (wire callbacks need it). Freeze/drain before capture/finish/cancel under normal operation lock; resume only if same active healthy session. GETs cached/no I/O. Add openapi and bounds validation, do not expose driver opcodes or motor addresses.

## Verification

- Red→green driver finite segment/speed direction/cancel/profile/held-axis/target-guard tests; default noarg not enabling.
- Fake-clock backend lease expiry, reordered keyup/start, late heartbeats, duplicate repeat, hardcap, settingchanges, auto mode/refusals, globalSTOP no resurrection and failed STOP ownership.
- GUI keys/input focus/multikey/repeat/focus/tab visibility/response-after-keyup and default/max parameters; FakeSerial only.
- Serial full pytest, JSsyntax/build/package byte checks and independent review. Root browser can inspect UI only; no real keyboard motion/enable without a separate user-directed safety check. Preserve current counts and do not auto-return.
