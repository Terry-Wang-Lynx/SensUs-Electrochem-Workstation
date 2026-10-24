# 高通量自动化实验 Implementation Plan

用户随后明确API-first/交互3D/逐列配置/可编程动作需求，本计划已由`2026-10-19-automation-api-v1.md`取代；不得继续按表单优先布局实现。

> Use subagent-driven-development/executing-plans. User scope and shared-bus topology confirmed; no commits/branches. Parent exclusively controls real hardware; this task performs no physical motor moves.

**Goal:** Add separate high-throughput tab with two V5.2 boards/eight named electrodes and shared-RS485 X/Z orchestration.
**Architecture:** Isolated motion driver, controller/planner, and frontend module. Existing single-board workflows unchanged and mutually exclusive.
**Tech Stack:** Python/Bleak/pyserial, standard HTTP server, vanillaJS/canvas, pytest.
**Spec:** `docs/superpowers/specs/2026-10-19-high-throughput.md`

## Global constraints / review focus

- One port/two unique nonbroadcast addresses; no auto enable/home/zero, no real moves by agent.
- Validate finite geometry/speeds/bounds, Z-safe before X, no silently clamped low speed, frame-address/ACK checks.
- Simulation touches no hardware and is visibly distinct. Live fail/cancel attempts all stops; never label unconfirmed stop as safe.
- Two unique BLE addresses, eight immutable board/channel identities, no shared acquisition with legacy controller.
- Stop cancels pending stages/cycles, human wait needs explicit resume, failure never resumes automatically.

## Task 1 — motion adapter (worker)

Own `pa_host/emm_motion.py` and `tests/test_emm_motion.py` only. Implement SharedEmmBus and validate_axis_config with TDD. Constructor `SharedEmmBus(port, baud, axes, serial_factory=None)` where axes X/Z have address,lead_mm,travel_mm,positive_direction,max_speed_mm_s. Methods connect()->snapshot,refresh()->snapshot,snapshot(),confirm_reference(),enable(axis),move_to(axis,target_mm,speed_mm_s,cancel_event=None),stop_all()->{stop_confirmed,axes,errors},close(). calibrations reset each connect; reference confirmation stores current encoder positions as software origin without hardwareZERO. Use provided reference protocol, verify uncertain ACK/status details where possible; no tests access real serial.

## Task 2 — independent page (worker)

Own new gui/high_throughput.js, existing gui/index.html/app.js/styles.css minimal nav integration, tests/test_high_throughput_gui.py. Add nav data-view=throughput, pages entry, dedicated section and script. No backend edits. API GET /api/high-throughput, GET /api/high-throughput/ports; POST /api/high-throughput/config,/preview,/start,/stop,/resume,/motion-connect,/motion-reference,/motion-enable,/motion-stop,/motion-disconnect. BLE scan reuse /api/devices/ble/scan, cached /api/devices. Default mode simulation; mode live explicit. Fields below. 8cards/curves, source board identity always shown, no simulated data represented as measured. Use textContent for user labels, no window.prompt/confirm dependency.

Config shape: {name,output_dir,boards:[{slot:'A'|'B',address,settings:{...v52settings}}],electrodes:[{slot:'A'|'B',channel:1..4,name}],motion:{port,baud,axes:{X:{address,lead_mm,travel_mm,positive_direction,max_speed_mm_s},Z:{...}}},recipe:{kind:'stationary'|'immersion'|'coating',columns:[1..12],cycles,duration_s,settle_s,drip_s,interval_s,withdraw_speed_mm_s,move_speed_mm_s,x_first_mm,column_pitch_mm,z_safe_mm,z_immersed_mm,manual_between_cycles,electrical_policy_confirmed}}. Default values e.g X1/Z2,lead2/travel150/50 are editable unverified placeholders, not frozen hardware. Config API returns {config,...snapshot}; snapshot {state,busy,mode,message,error,run_dir,cycle,column,step,stop_confirmed,events:[],devices:{motion:...},channels:[{slot,channel,name,time_s,current_nA,valid,latest_current_nA}],preview:[]}. Motion enable payload {axis}; start {mode:'simulation'|'live'}.

## Task 3 — planner/controller and integration (parent)

Own pa_host/high_throughput.py, gui_server.py, packaging/portable.spec, tests/test_high_throughput.py, docs. Preflight validates full plan before any hardware call. Live stationary launches two existing collect_ble child processes with unique outputs; fail/cancel one stops peer and requires terminal STOP confirmation. Mechanical modes gated by real driver/config/reference/enabled checks and explicit electrical policy. No analog calibration integration. Persist configs and run manifest/events; protect shared resource access and shutdown. Simulated lifecycle and curves tested without opening devices.

## Task 4 — acceptance

Run focused/new/full tests andJS checks; test page simulation/8mapping/stop. Independent review, fix important findings with regression. Build updatedsourcepackages; document only availablehardware proof. Update originalhardware repo'spointer; never change sourceCAD orreferencefolder.

## Ledger

- Scope: user requested tab, linked motion-design conversation, provided reference source, confirmedsharedRS485twoaddresses. Reference currentlysingle-axisQt prototype; adaptwireprotocol, notQtUIoruncheckedconnectbehavior.
- Task1 pending; Task2 pending; Task3 pending; Task4 pending.
