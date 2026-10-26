# Glove and AI diagnostic findings — 26 October 2026

## Immediate handover priorities
1. Ask hardware to check the response through the full comfortable bend on LEFT index GPIO34, LEFT middle GPIO35 and RIGHT middle GPIO35. These inputs hit the maximum readout in one tested pose. They are responsive, not proven disconnected, but clipping can hide part of the motion. Software normalization cannot restore information lost at a readout boundary. Check the circuit/divider, mounting/preload and consistent fit before final sewing; the data do not identify which of these is responsible.
2. Verify repeated identical bent poses, especially LEFT middle: its two held-bent medians were 0.6684 and 0.9053, compared with within-hold standard deviations of 0.0061 and 0.0107. Different pose depth or mounting/fit could explain the difference; this is not proof of a loose connection.
3. Both thumbs showed stable, clearly separated endpoints. Their smaller raw range is not the first hardware problem to fix. Do not force them to 0..1 just because their ADC fractions are near 0.4-0.6.
4. Confirm gyro calibration with the gloves stationary. In the initial triage IDLE period, LEFT gx had median about +15 deg/s (after omitting the first two seconds), while RIGHT was near zero. The user motion during IDLE is not verified, so repeat a stationary check rather than declaring a calibration fault.
5. Document sensor placement and any change in wiring/resistors/mounting, and preserve glove roles: LEFT 38:18:2B:17:E2:E6 / ID1; RIGHT 38:18:2B:14:EA:FE / ID2. New data should use the final stable hardware/mounting.

## Completed range checks: verified against all eight CSVs
Both uploaded reports have status measured. Every CSV hash matches its report; all samples are finite. Each trial has 101-102 samples over 2.000-2.020 seconds, no sequence discontinuities and source timestamp steps no greater than 22 ms. Per-hand source rates were approximately 49.975-50 Hz. No new submission failures were reported during either individual-hand range-check session; dropped and malformed counters stayed zero. Historical failure counters were nonzero but did not increase during these checks.

These are two repetitions per pose; they are not a complete reliability or gesture-accuracy test.

| Hand / sensor | Straight medians, repetitions 1 / 2 | Bent medians, repetitions 1 / 2 | Finding |
|---|---:|---:|---|
| LEFT index, GPIO34 | 1.0000 / 1.0000 | 0.3944 / 0.4974 | Responsive; straight pose at upper readout boundary; bent endpoint differs between repetitions. |
| LEFT middle, GPIO35 | 1.0000 / 1.0000 | 0.6684 / 0.9053 | Responsive; straight pose at upper boundary; large bent-pose repeat variation. |
| LEFT thumb, GPIO39 | 0.5565 / 0.5592 | 0.4650 / 0.4650 | Clear separation, no endpoint rail, consistent bent readings. |
| RIGHT index, GPIO34 | 0.3726 / 0.4059 | 0.6327 / 0.6527 | Clear response; no endpoint rail in these trials. |
| RIGHT middle, GPIO35 | 0.8791 / 0.8168 | 1.0000 / 0.9419 | Responsive; first bent trial is 1.0000 for every sample. |
| RIGHT thumb, GPIO39 | 0.5238 / 0.5275 | 0.3866 / 0.3748 | Clear separation and low within-hold noise, no endpoint rail. |

Pooled thumb endpoint separation corresponds to about 377 ADC counts on LEFT and 584 on RIGHT. Both thumbs decrease when bent; LEFT index/middle also decrease, while RIGHT index/middle increase. Different sign is not automatically a fault. If normalized bend values are introduced, use glove/channel-specific endpoints and version that preprocessing contract; do not silently reuse global calibration constants or old model normalization.

## Continuous triage capture
Input: triage_20261026T015437201923Z.zip. All four recorded data-file SHA256 values match summary.json. The run used the old RIGHT eleven-feature model hand_right_20261023_v3 and preserved all twelve channels in raw CSVs. The selected normalization.npz hash also matches the copy on GitHub. Model outputs came from laptop CPU inference only; this run measured neither Ultra96 nor phone/game output.

### Capture integrity and shutdown
The cue schedule ran from approximately 6.953 to 80.234 seconds on the capture clock. The RIGHT file ends at 80.328 seconds, but LEFT continues to 427.531 seconds. The summary has status failed / CancelledError and lacks final counters/statistics. Therefore this is usable diagnostic data, but NOT a cleanly completed or reconciled two-glove capture.

Only the shared cued period is used for the primary comparisons below. Later LEFT data must not be mixed into cue-based model analysis. The recorder has been updated with an explicit stop flag, bounded shutdown waits, counter preservation during interruption, and a higher-resolution inference timer. This addresses the observed shutdown behaviour but physical retesting remains pending. There is no need to recapture simply to preserve the already uploaded raw data.

### Reception evidence
Within the cued interval:
- RIGHT: 3,629 recorded samples, one sequence jump from a gap of 37 missing sequence values. It occurred around 13.906 s, during PICKUP, with a 758 ms source-time step. There was also a callback arrival gap of about 1.078 s in the full RIGHT file. We cannot locate the missing samples to radio vs firmware vs host conclusively without final source-stat reconciliation/serial evidence.
- LEFT: 3,506 recorded samples, 12 sequence discontinuities accounting for 88 missing sequence values, concentrated in the final IDLE block around 76.8-80.1 s. No boot change was recorded in either file.
- The full LEFT file includes 175 missing sequence values across its much longer duration. Subsequent range-check source statistics report 175 cumulative LEFT failures, while the initial triage snapshot was zero. RIGHT's cumulative failures rose from 6 initially to 45 by its later range check. Do not assume every later cumulative failure occurred inside the cue period.
- Steady callback-to-consumption backlog is generally small: cued-period median and 95th percentile were zero at the recorded clock resolution; maxima were 47 ms LEFT and 391 ms RIGHT. LEFT's separate startup backlog of 2.812 s occurred while setup was still connecting the other glove; it is not representative of steady-state reception.

The individual range sessions were clean while the continuous dual-hand capture had gaps. This warrants a later controlled simultaneous BLE test, not an assumption that a nominal 50 Hz rate proves no loss.

### Predictions and timing
There are 341 RIGHT model outputs in predictions.jsonl; 169 were displayed by the confidence/freshness filter. Logs preserve the remaining outputs. The initial IDLE cue had 18 top-1 CHOP outputs with mean confidence about 0.618, so the console showed none of them. An empty console does not establish correct IDLE detection.

After excluding the first 2.5 seconds of each action block to reduce previous-action boundary contamination:
- CHOP: 34 of 37 outputs had CHOP as top class.
- WASH: 34 of 38 outputs had CHOP as top class; only two had WASH as top class.
- DROP: only one of 40 outputs had DROP as top class.
- THROW: eight of 38 outputs had THROW as top class, with PICKUP/CHOP common.
- POURING: 22 of 38 outputs had POURING as top class, with PICKUP common.

These are descriptive output counts under intended cues, NOT classification accuracy: the actual frame-level actions/preparation/resets are not labelled, no video was supplied, and overlapping windows are correlated. Nevertheless, persistent confusion deep inside WASH/DROP blocks cannot be explained solely by a two-second boundary if the intended action was maintained there. Stable confirmation may suppress flicker/duplicates but cannot correct a sustained, confident wrong label.

Callback-to-result duration: median about 62 ms, 95th percentile about 125 ms, maximum 453 ms. This is time after laptop notification callback, NOT end-to-end BLE latency: source/laptop clocks are not synchronized. Recorded inference duration is quantized: median 0 ms, 95th percentile about 16 ms, maximum 234 ms. Do not interpret zeros as zero compute or use them as a precise speed benchmark. The updated helper uses perf_counter for inference duration while retaining the original callback-clock domain for alignment.

### Gyro range evidence
The firmware sensor module is configured for +/-250 deg/s. During the cued RIGHT data, gx/gy/gz repeatedly reach nearly identical positive/negative extrema; approximately 8.7%, 12.3% and 17.0% of samples respectively are within 0.03 deg/s of the observed extrema. This is consistent with gyro clipping after bias correction and is worth a stationary/controlled-speed review. It does not prove clipping is the main classification cause.

Do not increase the IMU range alone: the existing int16 gyro transport scale is 100, limiting transported physical values to approximately +/-327.68 deg/s. A wider range requires a coordinated scaling/protocol and data/model decision, otherwise clipping can merely move to transport packing. This is a software/hardware integration issue, not a reason for an unreviewed pre-handover flash.

## Working diagnosis and next steps
Multiple factors are present: changed hardware/mounting or sensor response relative to the old model, endpoint clipping/repeatability concerns, gyro-range/calibration concerns, some two-glove reception gaps, and rolling-window transition contamination. This evidence does not establish one single cause of last week's accuracy problems. It does show that simply shortening the window or raising confidence is not a sufficient demonstrated remedy.

Before handover: flag the three boundary-hit finger channels and LEFT fit/repeatability; photograph mounting; preserve raw captures and the full model directory. After handover: review videos if available, compare training/live distributions and replay fixed raw data, benchmark CPU timing accurately, add per-hand event/state confirmation with clear CHOP/WASH contracts, and coordinate v4/twelve-feature migration through laptop/Ultra96/phone/FPGA. Collect final training data only after hardware/mounting/scaling choices are stable.
