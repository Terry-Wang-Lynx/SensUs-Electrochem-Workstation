# API-first 高通量自动化 Implementation Plan

> Use subagent-driven-development/executing-plans. Latest user specification supersedes earlier form-first plan. No commits/branches/pushes; parent owns real hardware; no physical motor operation during this work.

**Goal:** Central interactive3D+column editor and agent-friendly action API for eight electrodes, twoBLE boards, sharedRS485X/Z.
**Spec:** `docs/superpowers/specs/2026-10-19-automation-api-v1.md`
**Architecture:** Motion driver, declaration compiler/controller, separate3D frontend; explicit integration/ownership guards in old server.
**Tech Stack:** Python/Bleak/pyserial, stdlibHTTP, bundledThree.js, vanillaJS, pytest.

## Constraints / review focus

No arbitrarycode execution, no automatic motion onconnect/drag/preview; unknowncalibration blocksreal motion. Addresses unique, twoBLEidentitiesdistinct, masksreal. Defaults/overrides deterministic and fingerprinted. No failureauto-resume; stopboth boards beforemove. Oldsingle-board defaults unchanged. Simulatedrecords unmistakablysynthetic. Preserve allprevious0.5.0changes andtests.

## Ownership

1. Motion worker: ONLY `pa_host/emm_motion.py`, `tests/test_emm_motion.py`; sharedbusAPI frompreviousplan (connect/refresh/snapshot/confirm_reference/enable/move_to/stop_all/close), optional event_callback forwireaudit. No hardwareaccess. TDD responsebinding,ACK,mechanical validation,lowRPMreject,reference/enable gates,cancel/stale/timeout. Reference source read-only.
2. Backend worker: ONLY new `pa_host/automation.py`, `tests/test_automation.py`; `default_experiment(output_dir='')`, `compile_experiment(document)->{experiment,plan_hash,steps,warnings,duration_s}`, `experiment_schema()`, `openapi_document()`, `AutomationController(state_dir,runs_dir,project_dir)` with snapshot/is_busy/save/validate/start/stop/resume/events/ports/connect_motion/reference_motion/enable_motion/stop_motion/disconnect_motion. Controller uses motionworker API and parentextendedcollect_bleCLI. Expose `AutomationError(code,message,path='',status=400)`; parentmapsHTTP. Implement typedactions,runmanifest/provenance,simulationfast enoughfortest,live twochildren gated. No edits tooldserver orcollector. Everystep `id/op/column/resolved_params` hasstablefingerprint. Source staticdefaults/model refs fromspec, no hardwaretesting.
3. Frontend worker: ONLY `gui/high_throughput.js`, `gui/high_throughput_3d.js`, minimal edits`gui/{app.js,index.html,styles.css}`, assets`gui/ht-geometry.json`,`gui/three.min.js`,`gui/three-LICENSE.txt`, new`tests/test_automation_gui.py`. Main navigationdata-view=throughput; centralThreeJS orbitableapparatus +12clickablecolumns, sideproperties/defaultsoverrides andJSONprogrameditor, actioninsertcontrols,timeline,preview/run/stop/waitresume,A–Hrecords. UseAPIprefixfromspec. VendorThree0.160.1fromnpmregistry andMITlicense; reusegeometry.json in siblingautomationproject plusreferencepose, keepgeometryreferenceunmodified. No networkdependencyatruntime, noGPUcaptures orbrowser/hardwarecalls (parentdoesQA). Camera/columnclicknopersistenceorhardwarecommandsuntilexplicitaction. No fakeGaussianSplatclaims.
4. Parent: collect_ble mask/startgate backwardscompatibility+tests, gui_server routing/resource/shutdown integration, packagingassetmime/package-data, docs/APIexamples, finalsimulationUIQA/fulltests/review. Do notchangefirmware ormovehardware. Real motorvalidation remainspending.

## Collector extension contract

Optional `--channel-mask` intdefault15 and `--start-gate` Path. Validate1..15 andanalysis_channelenabled; START usesmask, requireonlyexpectedchannels, rejectunexpectedchannel. GatefileJSON {run_token:<childtoken>} onlyreleasesmatchingchild; whilewaitingphase=configuring/config_confirmed=true; STOP/cancelbeforegateneverSTART. Boundedgatewait60s. Parentbackendcreatesseparategatefile/tokenperboard, polls bothconfigs thenwritesgates. Acquiringoneboardonlywhenchannelsmaskother0 skipsotherchild butstilldistinctbindingsforliveexperiment.

## Verification / ledger

- Userpriority: interfacefirst,3Dscene/Scratchlikeactions,percolumnsolutions,dwellselectedchannels,allrawrecords/provenance/logs; sharedUSB-RS485withdifferentaddressesconfirmed.
- Writefailingtests→implement→focusedgreen perownedtask; parentfullregression/JSsyntax/packagebuild.
- Read-onlyindependentreviewforprotocolsafety,ownership/motiongates,traceabilityandUIclaims.
- No dualboard/axes hardwareclaimwithout actualtest; defaultsimulateonlyduringdevelopment.
