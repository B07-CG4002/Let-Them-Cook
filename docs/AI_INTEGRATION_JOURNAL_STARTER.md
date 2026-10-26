# AI / glove integration journal starter

Team request relayed in chat: entries from Week 9 onward, ideally about one page per week, covering integration activities, failures, investigation, alternatives, changes, tests/retests and cross-subsystem decisions. Verify the official rubric and week/date ranges before final submission. This starter has not been inserted into the shared document.

## Weekly entry template
- Week and dates:
- Objective / subsystems involved:
- Problem and reproducible trigger:
- Evidence (log, report, recording, commit):
- Investigation / alternatives considered:
- Change and reason:
- Verification actually completed (environment, sample counts, results):
- Limitations / outstanding tests:
- Team decisions and next steps:

## Evidence available from this chat — allocate to the correct weeks

### BLE startup/authentication
Problem: Windows connected and completed GATT operations, but the rate command timed out and no sensor packets arrived. Logs showed authentication completing before the connection callback, then authenticated=0 and suppressed samples.
Change: retain a qualifying authenticated event briefly for the same peer, consume once, clear on disconnect/bond erase; retain SC+MITM+bond requirements. Pin espressif32@6.12.0 for ESP-IDF 4.4 compatibility; enable continuous IDLE transmission.
Verified on the then-tested RIGHT board: 1,009 packets in 20.14 s (about 50.1 samples/s), no observed sequence discontinuities/errors in that run. Do not claim all later hardware or two-glove end-to-end paths passed based on this run.

### Independent-hand AI redesign and dataset revisions
Reason: each hand must support different simultaneous actions, which one combined paired-hand label cannot express.
Change: independent eleven-feature per-hand recording/training/live baseline, seven labels (six actions plus IDLE), explicit metadata/quality checks, GUI preparation/cancellation fixes. Revised DROP to include lowering/release and THROW to chest-height forward motion for the pilot.
Evidence: old eleven-feature right-hand v2 validation 21/21 and test 28/28; right-hand v3 validation 21/21. Small cued sets and reused test recordings do not establish continuous live generalization. Do not invent a v3 test score from a report filename.
Live limitation: two-second rolling windows at 0.2 s updates also classify preparation/return movements and produce repeated outputs. Confidence filtering alone cannot prevent confident transition errors. Per-hand confirmation/event/state handling is planned; it has not been demonstrated as implemented in these updates.

### Three-flex migration and hand identity correction
Hardware mapping: index GPIO34, middle GPIO35, thumb GPIO39. Additional channel changes model input to twelve features per hand.
Change: opt-in v4 forty-byte BLE packets, LCS4 source statistics, new firmware targets, separate src.hand_ai_flex3 package with twelve-feature recordings/preprocessing/CPU model configuration. SDK pin and authentication checks retained. Existing common v3 bridges and FPGA deployment were not migrated by this local recorder work.
Issue: the physical LEFT glove initially ran a RIGHT ID2 build; temporary address changes compounded the confusion. Physical finger/glove checks and source statistics were used to distinguish board address, hand role, packet format and firmware device ID. Final intended mapping remains LEFT 38:18:2B:17:E2:E6 / ID1 and RIGHT 38:18:2B:14:EA:FE / ID2. User reported LEFT connection working after correcting its firmware; capture the final RIGHT verification evidence separately.
Host verification of the three-flex package: 68 tests run, 66 passed and two PyTorch training tests skipped here; eight portable firmware binaries passed; actual sensor code was exercised with simulated ADC/I2C in both dimensional modes. The user subsequently reported a successful ESP32 build/upload; distinguish upload success from live sensor and full-system validation.

### Range measurement checkpoint (new helper)
New guided diagnostic measures straight/bent endpoint distributions for both hands, within-hold noise, repeated-pose variation and ADC-boundary readings. It records measurements and candidate scaling only; firmware/model scaling is not changed. Eight automated diagnostic tests passed, including simulated both-hand capture and failure cleanup. Physical measurement results and any calibration decision remain to be added after the user's run.

## Outstanding integration evidence
- Final physical sensor mapping/ranges and stable mounting after sewing/casing.
- Sustained, battery-powered, simultaneous two-glove BLE reception.
- New twelve-feature per-hand datasets, validation/test results and continuous testing.
- Confirmed CHOP counting and WASH interruption contract; implemented postprocessing tests.
- Coordinated v4/twelve-feature laptop/Ultra96/phone migration.
- Matching final model weights, export, FPGA build and board agreement for those exact weights.
- Actual worker launch command and end-to-end app results.
