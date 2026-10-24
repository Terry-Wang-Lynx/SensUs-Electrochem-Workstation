# Guided High-Throughput Automation Implementation Plan

> For agentic workers: use subagent-driven-development for these bounded tasks, with tests first and an independent final review. No commits or new branches.

**Goal:** Make the normal automation page a simple template-and-calibration workflow with fixed physical 1/12 calibration and independent BLE recovery.

**Architecture:** A focused guided document/calibration service translates the user template into the existing action compiler. Explicit schema 1.1 adds nullable concentration, signed pitch, channel windows and guided fault policy without changing legacy 1.0 semantics. The controller retains ownership and audit truth across per-board reconnect attempts; the UI presents a small normal workflow while keeping raw controls/API in Advanced.

**Tech Stack:** Python stdlib HTTP/threading, pytest, vanilla JavaScript, local Three.js.

**Spec:** `docs/superpowers/specs/2026-10-22-guided-automation.md`

## Global Constraints
- No physical serial/BLE operations, firmware writes or motion in this implementation/validation run.
- Preserve existing uncommitted 0.5.0 work, legacy v1 programs, original CAD, V4/V5.1, STOP ownership and audit failure handling.
- Physical columns 1–12; fixed calibration 1 and 12; electrode identities A–H never renumbered.
- Missing data stays missing; no fabricated concentration, interpolation, zero filling, hardware-sync or analog-accuracy claims.
- Movement defaults are editing presets, not newly hardware-qualified collision-safe settings.

## Review Focus
- Negative X calibration pitch and selecting one/intermediate/noncontiguous columns must not change physical spacing.
- Cancel, restart or manual enable must never silently move to an old target; unknown reference prevents live runs.
- A disconnected board cannot stop a healthy peer or extend the column deadline; retry data must belong to the correct attempt/window.
- Two disconnected boards require lift and recovery waiting, while STOP remains unknown for unreachable outputs.
- Invalid windows, stale UI drafts, disk faults and repeated start requests must not bypass guards or lose sample provenance.

## Task 1: Guided document and manual calibration
**Ownership:** `guided_automation.py`, `emm_motion.py`, `test_guided_automation.py`, calibration-specific motion tests.
**Interfaces:** `default_guided_experiment(output_dir='')`; `GuidedAutomation(state_dir)` exposing `snapshot()`, `save(document)`, `compile(document, mode='simulate', motion=None)`, `begin_calibration(motion)`, `capture_calibration(motion, point)`, `finish_calibration(motion)`, `cancel_calibration(motion)`. Compile returns the existing compiler result. Point IDs `first`, `last`, `safe`; first captures working Z too. Saved calibration belongs to exact motion settings/reference counts; no automatic calibration.
- [ ] Add failing tests for default 12 columns/A–H, selected 2/7/11 spacing, one-column case, nullable concentration, invalid windows and fixed physical endpoints.
- [ ] Implement strict guided document with `schema_version: 'guided-1.0'`, `name`, `output_dir`, `bindings`, `safety`, `defaults` (movement legacy values plus `dwell_s`), `columns` ({index,name,enabled,duration_s,windows}), `electrodes` ({id,name,enabled}), no user-supplied trusted calibration.
- [ ] Add failing fake-serial tests for explicit release/capture/relock, old target suppression, cancellation and persistence. Implement manual driver primitives without homing/zeroing or movement and guarded reference restore; errors leave a blocked/unknown state.
- [ ] Compile to schema 1.1: same v1 shape plus `run_policy: 'guided'`, `calibration_id`; nullable concentration; dwell `windows` map `{channel:{start_s,duration_s}}`. Simulated geometry is explicitly synthetic and cannot authorize live runs.
- [ ] Run targeted tests; report red/green evidence and hardware limitations.

## Task 2: Isolated per-board acquisition and pause
**Ownership:** `automation.py`, `collect_ble.py`, runner/collector tests.
**Interfaces:** Existing v1 controller unchanged; schema 1.1 fields as Task 1. Add `pause()` with pause effective only after withdraw and `resume()` for normal/recovery waits. State exposes column timing and per-board recovery, attempt IDs/gaps. Domain/UI consume these fields, not child internals.
- [ ] Write failing tests for signed pitch/null concentration/windows with strict v1 compatibility and plan hash coverage.
- [ ] Implement schema 1.1 compiler support and synthetic window filtering; retain raw trace/audit outside effective windows but exclude it from effective sample records.
- [ ] Write deterministic child-runner tests for START-ACK clock, single-board loss/healthy peer, retry at100/180 with80 remaining, stale attempt data, both-lost/lift/wait and pause-after-lift.
- [ ] Implement guided-only retry runner using distinct attempt directories/tokens, bounded retry/backoff, shared nonextending row deadline, per-board START timestamps and explicit gaps. Recoverable BLE failures do not mask audit/motion/cancellation failures. Unknown STOP ownership persists; no lateral transfer with unknown board output unless recovery/STOP is confirmed; safe lift is allowed by user policy.
- [ ] Run targeted regression tests, including old v1 fail-fast behavior and STOP/disk-fault cases.

## Task 3: Guided UI and electrode visibility
**Ownership:** `gui/high_throughput.js`, `gui/high_throughput_3d.js`, guided CSS, `test_automation_gui.py`, `test_automation_geometry.py`.
**Interfaces:** GET `/api/automation/v1/guided` → `{document,calibration,manual}`; POST `guided/experiment` `{document}`; `guided/preview` `{document,mode}` → compiled; `guided/run` `{document,mode,plan_hash,idempotency_key}` → state; `guided/calibration/{begin,capture,finish,cancel}` (capture `{point}`); POST `pause`; existing state/events/motion routes remain.
- [ ] Write failing pure-model tests for selections, names, duration inheritance, advanced delays, draft invalidation and default collapsed Advanced.
- [ ] Implement primary template/column/electrode controls, local edits, auto preview before run, explicit live confirmation, a calibration panel shown only by click, clear results/status, pause/resume. Keep agent JSON/API and hardware/movement settings Advanced.
- [ ] Add `setElectrodes(electrodes)` to CAD scene and tests proving removed electrode/wire visibility only, fixed carrier and source mesh positions unchanged.
- [ ] Run Node syntax and UI/geometry tests; report IDs needed for parent browser QA.

## Task 4: Server integration and acceptance
**Ownership:** `gui_server.py`, `index.html` cache keys if needed, documentation, packaging/evidence, pA AGENTS status.
- [ ] Add failing HTTP tests for guided routes, structured errors, busy/manual calibration exclusivity, required calibrated live runs and idempotency hashes.
- [ ] Integrate guided service into existing controller ownership/operation lock; do not allow legacy run/reference/motion actions to bypass active manual calibration. Persist guided document and calibration without leaking transient state.
- [ ] Run targeted tests then full suite, JS syntax and wheel/sdist asset checks.
- [ ] Start isolated BLE-mode local server with test state; browser-test selections, labels, calibration entry/cancel errors and short synthetic run; inspect screenshot and save evidence.
- [ ] Independent code review against spec; fix actionable issues, rerun affected/full tests, update docs with exact verified vs hardware-pending boundaries.

## Execution Ledger
- Ruling: reuse the existing user-authorized checkout and preserve all uncommitted work; no new worktree/branch because current native environment and packaging depend on this checkout.
- Ruling: manual calibration may not claim verified no-jump motor behavior from offline tests. Supplied Emm documentation has no qualified target-reset/relock guarantee, so the real driver rejects manual begin before disabling motors; the full workflow is tested with an explicitly capable fake. Hardware qualification remains required before real manual calibration.
- Ruling: safe lift after board loss is allowed; subsequent lateral travel still requires every unknown output to be confirmed stopped. This can wait longer than the requested at-least-one-board recovery, but avoids silently moving an electrode whose output remains active/unknown.
- Integration tests: 13 new guided-route failures reproduced before route implementation; 15 guided integration tests and the prior 10 integration tests pass at this stage. Portable hidden-import regression red→green (4 packaging tests). Full suite/GUI pending concurrent tasks.
- Tasks1–4 implemented and independently reviewed; core findings fixed with red→green regressions. Final997-test run passes. Real manual relock and dual-board hardware acceptance remain explicitly unqualified, not bypassed.

## Task 5: Follow-up shared sampling and truthful BLE status
- [x] Add failing pure UI tests for copying exactly five common sampling fields to both board settings while preserving UUID/analysis identity and rejecting invalid DAC/range/offset atomically; implement collapsed sampling/mapping tabs.
- [x] Clarify template default vs column override, and trust only service `calibration.usable`.
- [x] Reproduce 8 missing BLE-status cases, then implement explicit scan metadata, Bluetooth-enable/permission guidance and last-scan count without automatic radio calls.
- [x] Verify eight-channel mapping using two distinct bound UUIDs, identical protocol names and deliberately reordered CH4/CH1/CH3/CH2 frames; identities must follow packet channel, not arrival order.
- [x] Independently review/fix STOP/reference race, post-STOP checkpoint handoff, orphan attempt STOP recovery, corrupted calibration startup and controller/service lock order with failing-then-passing regressions.
- [x] Reproduce delayed recovery beyond attempt32 in a600s column; continue capped-backoff retries until the fixed deadline instead of silently giving up.
- [x] Final997 tests pass; current server/API and CUA checks complete, wheel/sdist built and runtime assets byte-matched. Hardware was not operated. Acceptance evidence is `artifacts/guided-validation-20261022/`.
