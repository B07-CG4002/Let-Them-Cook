# Immediate pre-handover diagnostic

This is a local BLE -> Windows -> CPU CNN diagnostic. It sends no game commands and does not measure Ultra96/phone latency. The shared repository still uses 11-channel v3 / LCS3 ingestion; new gloves use 12-channel v4 / LCS4, so that integration needs coordinated updates before a valid whole-app test.

Close all other BLE clients/GUIs. Power both gloves with three-flex firmware, keep them still during boot calibration, and wear them normally.

From repository-root PowerShell:
```powershell
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\pre-handover-triage.zip" -DestinationPath . -Force
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.triage_capture --hand both --run models/hand_right_20261023_v3 --legacy11
```

The old right-hand checkpoint is an explicit diagnostic baseline: only the first 11 channels go to its CPU model, while raw CSVs retain all 12. New mounting, fit or sensor response may differ from last week's training conditions. This does not validate a 12-feature model, FPGA deployment, or explain all historical errors on its own. If the checkpoint cannot load, the tool prints the error and still captures raw data. If only one glove is available, use --hand right. Do not use a right-hand model as evidence of left-hand accuracy.

Capture lasts about 73 seconds AFTER connections finish, with eight cues: IDLE 6s, PICKUP 10s, DROP 10s, CHOP 10s, THROW 10s, WASH 10s, POURING 10s, IDLE 6s. Use RIGHT for the actions and both hands for WASH. For PICKUP and DROP, naturally reset your grip/pose before repetitions. Complete an action, pause, reset naturally, then repeat. Cues describe intended tasks; they are not frame-level ground truth for every preparation, action and return.

Record video of the hands plus cue display or audible beeps if possible. Do not start a second BLE program to record predictions. For a second run (only if time), emphasize one action at a time with pauses; keep the model/window/threshold settings unchanged between runs to preserve a baseline.

Outputs are timestamped under data/hand_flex3_checks/triage_<UTC>/:
- left_raw.csv and right_raw.csv: all 12 channels, device/boot/sequence/source uptime, callback and consumption times relative to one laptop clock origin.
- predictions.jsonl: every model prediction, probabilities, ending source sequence, intended 2s source-window boundaries, CPU inference duration and callback-to-result duration. Console shows only confidence above 0.70 with non-stale results; the log preserves rejected low-confidence results for diagnosis.
- cues.jsonl: local cue times and durations.
- summary.json: model file hashes, hand addresses, starting/ending source statistics, loss/reset indicators and health counters.

Callback-to-result time is NOT one-way BLE latency. Sensor uptime and laptop clocks are not synchronized. This capture has no app-display or Ultra96 timestamps. Window-start uptime is inferred from the 2s requested window; account for interpolation and source continuity during replay. Counters distinguish sequence gaps, source-time gaps, boot changes, queue drops and malformed packets; zeros do not prove the whole pipeline loss-free without source-stat reconciliation. Partial failures are saved and must not be called successful captures.

After the capture, package the latest folder:
```powershell
$latestCapture = Get-ChildItem .\data\hand_flex3_checks -Directory -Filter "triage_*" | Sort-Object Name -Descending | Select-Object -First 1
Compress-Archive -Path "$($latestCapture.FullName)\*" -DestinationPath "$env:USERPROFILE\Downloads\$($latestCapture.Name).zip" -Force
```

Send that ZIP and the video later. Inspect raw source timing, sensor range, model confidence/confusions, boundary windows and inference timing separately. Replaying identical sensor recordings allows software/window/postprocessing experiments after handover. Shortening windows changes the model-input distribution; do not attribute any improvement to latency without an appropriate matched model/protocol comparison. Do not tune on an untouched test set.

Before handover, photograph sensor mounting, note glove labels/flex mapping and firmware roles, preserve the complete trained model directory including model.pth/config/normalization/class order, and record any mechanical changes requested. Fit/mount changes need a later recheck. No firmware, model weights or scaling are changed by this helper.

Validation here: two automated tests passed, including a simulated both-glove capture that writes raw streams, action cues and model predictions and closes both connections. Physical Bluetooth operation is not verified here.
