# Live Feedback and Digital Twin Plan

**Goal:** Report real high-throughput connectivity and drive rigid CAD groups from verified raw feedback, never from targets.
**Architecture:** Controller read-only feedback worker and cached device-status adapter; independent geometry/limit-bound twin registration service; global sidebar plus feedback-priority scene API. Separate physical raw capture from operator-declared CAD coordinates, preserving immutable motion guards.
**Tech Stack:** Python stdlib/pytest, existing Emm driver, vanilla JS/Three.js.
**Spec:** `docs/superpowers/specs/2026-10-23-live-feedback-twin.md`.

## Constraints / Review Focus
- No enable/release/motion/home/config writes; only authorized connection and existing-bus reads. Restart cleanup may issue explicit normal STOP; report it separately.
- Existing uncommitted1086-test baseline preserved; no branches/commits/push. Current checkout reused by explicit user continuation.
- Hand-turn feedback is valid displacement, not an automatic reset; don't require old parked counts equal new counts.
- Freshness/fault/time/identity truth; no USB-enumeration-as-motor-connect, no bound-address-as-BLE-link, no simulated-as-physical pose.
- Lock order controller→service/driver; thread shutdown must not deadlock connection/STOP; GET never performs hardware IO.
- CAD coordinates lack mechanical endpoint anchors; require explicit finite CAD correspondence and physical-span validation, never guess or rescale.

## Tasks
### 1: Device feedback (worker)
Own `automation.py`, new `automation_devices.py`, tests. Controller ctor `motion_feedback_interval_s=0` opt-in (AppState0.5); start read-only worker on explicit successful connect, stop on disconnect, no automatic open. `refresh_motion()` and `device_status(board_bindings=None)` cached adapter. Output `{motion:{connected,port,baud,faulted,axes:{X/Z:{address,raw_count,speed_rpm,feedback_age_s,telemetry_valid,enabled,referenced}},feedback_error},boards:{A/B:{address,state,channel_ids}},limits:{state,active}}`. Simulated/historical board status cannot claim active links. Test read-only whitelist, freshness, busy skips, fault/error, STOP/disconnect lifecycle and exact identities.

### 2: Twin registration (worker)
Own `digital_twin.py` and tests. `DigitalTwin(state_dir,geometry_path)` with `snapshot(motion,limits)` and `register(motion,limits,cad_bounds,confirmed=False)`. `limits` is trusted service snapshot withactive record; `motion` iscached motion dict withper-axis ages/raw/profiles. Pure snapshots, no drivercalls. Output `{state,registered,fresh,pose,registration,error}`; pose only for synced: `{scene_x_mm,scene_z_mm,x_center_mm,lift_mm,feedback_age_s,source:'encoder_feedback'}`. Strict persistence and CAD/source/limit fingerprint checks; preserve 1mm/mm with no scaling; tolerance0.1mm explicitregistrationmetadata. Tests both signs/countzero/handmovement, normalSTOP, stale/errors/reconnect/corruption/mismatch, no writable counts.

### 3: Sidebar and CAD feedback (worker)
Own GUI `app.js,index.html,styles.css,high_throughput.js,high_throughput_3d.js` +frontendtests. Global GETdevices polling, independent low-left cluster, actual connectivity vs guard status. Four CAD correspondence inputs in/near existinglimits workflow (advanced, initiallyblank); POSTtwin/register only declarativeCADvalues+confirmation, no additional physical pose move. Real attached mode disables plan animation from overwriting scene; live pose uses dedicatedscene setter and feedback timestamp, unregistered/stale/fault explicitoverlay. Mouse camera remains. Preserve draft/save/limits safety flows. Tests actualhandlers and group transforms; parent solebrowser/hardware QA.

### 4: Parent server/integration/QA
Own `gui_server.py`, packaging/docs, tests and actual read-only check. AppState workeroptin, Twin ctor/service routes, trusted currentlimits only, payloadvalidation andoplock busy protection. Offline real-service/emulated tests, whole suite serial, isolatedbrowser, build/bytechecks, independent review. Explicitly leave actualgeometry/limits uncalibrated unless user captures them; no fake live pose.

## Ledger
- Initial physicalappconnect success: X1/Z2,0RPM/status03,−5/1, port−110; read-phaseevents backed up at `artifacts/20261023-rs485-app-connect/`.
- User corrected presumed reset: count changes came from hand-turn. Recorded as valid motion; no old reference silently installed.
- Read-onlyCADresearch confirms rawbounds needCADanchor. Integrate correspondence into existingfour-point workflow, no additional physicalalignment sequence; unprovidedvalues stayunknown.
- Tasks pending.
