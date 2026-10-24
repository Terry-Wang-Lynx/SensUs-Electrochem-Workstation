# Absolute Motion Limits Implementation Plan

> For agentic workers: use subagent-driven-development; user authorized starting implementation. Tests first, no commits/branches or physical hardware actions.

**Goal:** Capture four manual operating boundaries and enforce them independently of experiment coordinates throughout every real workstation run.

**Architecture:** A separate persistent `AbsoluteMotionLimits` service owns captured raw-count records. The driver enforces installed immutable raw bounds and exposes read-only capture/monitor primitives. The controller activates strict policy for AppState, validates whole plans and monitors non-motion phases; UI/API cannot inject trusted bounds.

**Tech Stack:** Python stdlib, pytest, vanilla JavaScript/Three.js, existing Emm RS485 driver.

**Spec:** `docs/superpowers/specs/2026-10-23-absolute-motion-limits.md`

## Global Constraints
- No physical serial/BLE connections, motion, releases, enable, zeroing, firmware writes, commits or pushes this turn.
- Preserve all existing uncommitted 0.5.0 work and previous997-test baseline.
- Four named boundaries; static raw coordinates survive logical reference changes, not unverified raw-counter resets.
- STOP/audit truth and original strict STOP detection remain; hardware relock remains unqualified.
- Reuse current explicitly authorized checkout, no separate worktree/branch.

## Review Focus
- New reference origins, signed direction and pulse quantization cannot enlarge captured raw bounds.
- Missing/corrupt/stale records, changed ports/addresses/configs and reconnects fail closed for live motion without breaking read-only connection/STOP.
- Captures require stationary telemetry and one exact connection; wrong physical edge order/identical points/client-supplied counts are rejected.
- Controller/service/driver/wire-callback lock order and STOP during capture/save/preflight do not deadlock or silently rearm.
- Uncommanded-axis drift and telemetry failure during dwell/BLE recovery stop motion and retain resource ownership; simulation remains usable.

## Task1: Captured records and low-level enforcement
**Owner:** `motion_limits.py`, `emm_motion.py`, `test_motion_limits.py`, `test_emm_absolute_limits.py`.
**Interfaces:** `AbsoluteMotionLimits(state_dir)` with `snapshot(motion=None)`, `begin(motion)`, `capture(motion,boundary)`, `save(motion,clearance_confirmed=False)`, `cancel(motion)`, `require(motion)`, `preflight(motion,steps)`; snapshot `{state,active,calibrating,record,draft,error}`. Driver `capture_limit_position()` returns exact profile+connection identity and stationary raw counts; `install_absolute_limits(record)`, `check_absolute_limits()`; constructor keyword `require_absolute_limits=False` preserves standalone library compatibility, but AppState-created drivers require true. No payload can set this flag.
- [ ] Write and observe failing tests for four-point capture/order/strict validation, persistence/reconnect/corruption, changed reference, requested+quantized out-of-bounds, untouched axis drift, STOP races and no-write capture.
- [ ] Implement immutable raw-count guards, endpoint tolerance margin, stationary read-only capture and connection/profile-bound authorization. Driver move loop checks both axes; capture bypasses old bounds only in an explicitly exclusive no-motion workflow.
- [ ] Run owned focused tests and prior driver tests; report exact record schema and callback/locking expectations.

## Task2: Controller policy and continuous checks
**Owner:** `automation.py`, controller-specific tests in `test_automation_limits.py`.
**Interfaces:** Controller constructor `require_absolute_limits=False` for headless legacy compatibility; production AppState passes true. Controller owns `limits=AbsoluteMotionLimits(state_dir)`. Expose `limits_snapshot/begin/capture/save/cancel` methods and `_limit_calibration_active` in busy. Bind driver strict policy during production connect. Before live move plans, `limits.require/preflight`; runtime monitoring/driver checks stop on limit violations. Public `limits_snapshot()` never reads physical hardware.
- [ ] Add failing fake-bus tests for mandatory production gating on both schema versions, all-target preflight, busy/exclusivity, no JSON override, dwell/drift/failure monitoring, STOP/race cleanup and no fabricated confirmed STOP.
- [ ] Implement bounded monitoring with consistent controller→service→driver ordering, preserving original legacy collectors/STOP and simulator behavior. Record violations as errors, not normal cancellation.
- [ ] Run focused controller regressions; do not launch competing full suites.

## Task3: Manual four-boundary UI
**Owner:** `gui/high_throughput.js`, automation CSS only, UI-specific tests.
**Interfaces:** exact API paths in spec. `limits` data is independently polled and never included as writable experiment JSON.
- [ ] Add failing model/real-handler tests for explicit start, four captures, clearance checkbox, save/cancel gating, running/stale/corrupt states, preserved experiment draft and no automatic physical commands.
- [ ] Add normal folded limit panel with four physical labels and clear software/hardware warning; no numeric bound editing, no auto release/homing or bypass switch. STOP remains enabled.
- [ ] Run focused GUI tests and JS syntax; report IDs for parent browser QA.

## Task4: Server and offline acceptance
**Owner:** parent `gui_server.py`, packaging/docs/tests, cache keys.
- [ ] Test routes/structured errors, production flag, unknown counts and busy/STOP exceptions before integrating.
- [ ] Integrate AppState strict policy and limit routes under existing operation lock; manual/limit capture states cannot bypass each other's guards.
- [ ] Run997 baseline plus new tests serially, JS/Python checks, isolated simulator/browser QA, rebuild and byte-check package assets.
- [ ] Independent review, fix with regressions, record hardware-pending boundaries and leave current physical X/Z pose untouched.

## Ledger
- User confirmed manual capture and explicitly asked to begin; no additional routine approval required.
- Unanswered mechanical clearance becomes a mandatory operator save checkbox, not an assumed safe distance.
- Strict policy is mandatory in production AppState; optional constructor flags preserve library/testing compatibility only and are not exposed in JSON/API.
- Task1 complete:134 focused driver/service/prior-driver tests. Task2 complete:21 new controller tests plus old regression coverage, immutable per-run limit record added. Task3 complete:four-boundary UI and real-handler coverage, disconnected prompt added. Task4 complete:strict production policy, guarded routes,1086 full tests, browser/API/offline-serial workflow and independent review passed.
- Runtime monitor stops axes before publishing cancellation so collector cleanup cannot delay the physical stop request. STOP arriving during plan preflight is not cleared before start. Both issues have failing-then-passing regressions.
- No physical connection, drive, release, firmware operation or Git integration performed. Real boundary captures remain unset, not synthesized.
