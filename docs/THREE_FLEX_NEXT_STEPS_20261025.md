# Continue the three-flex AI pipeline — 25 October 2026

## First checkpoint: measure the gloves
This small add-on needs the existing three-flex package. It adds a guided terminal measurement tool, tests, and this guide. It does not replace your GUI, modify firmware, change BLE mappings, or apply calibration.

From repository-root PowerShell after downloading flex-range-check.zip:
```powershell
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\flex-range-check.zip" -DestinationPath . -Force
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_flex_range_check.py" -v
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.flex_check --hand both
```
Close recording GUIs/live inference/other BLE clients first. Power both gloves, USB or batteries. Both require the new three-flex firmware: LEFT = address 38:18:2B:17:E2:E6 / ID1 / LCS4; RIGHT = 38:18:2B:14:EA:FE / ID2 / LCS4. This command explicitly supplies these addresses and still verifies source identities; it does not rely on any temporary ble.py mapping changes.

If checking one glove:
```powershell
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.flex_check --hand left
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.flex_check --hand right
```

The tool asks you to hold fingers comfortably straight, then comfortably bent, three repetitions. Press Enter after reaching each pose; there is a three-second countdown, then a two-second capture. For --hand both, hold the pose on BOTH gloves. Do not force a sensor into an uncomfortable bend. This measures endpoints of a comfortable pose, not a physical angle. It cannot automatically prove finger mapping: verify flex1=index/GPIO34, flex2=middle/GPIO35, flex3=thumb/GPIO39 by moving each finger individually in the existing GUI.

Outputs: data/hand_flex3_checks/flex_range_<UTC timestamp>/range_report.json plus a separate raw CSV for each hand/pose/repetition. Send the JSON for review. All outputs are hardware checks and must remain outside the training input folder.

The report includes median endpoints, 10th-90th percentile bands, within-hold noise, across-repeat median variation, ADC-boundary frequency, and approximate endpoint separation in 12-bit ADC counts. Raw transport values are ADC/4095 after int16 transport scaling. Example: a 0.4 to 0.6 separation is about 819 counts; the useful signal depends on noise and repeatability, not reaching 0 and 1.

REVIEW warnings are descriptive heuristics, not a certified pass/fail threshold. The separation-to-noise warning uses three times within-hold standard deviation or three ADC counts. Candidate endpoint scaling is reported only; nothing is saved into firmware/model normalization and no values are changed. Do not force constant or railed readings to fill 0..1: determine their actual response first.

Reception quality is checked inside each captured two-second trial: finite twelve-channel values, sequence continuity, source timestamps and fresh arrivals. Source rates use (N-1)/elapsed source time. Submission-failure counters cover the connected measurement period. This is not a 30-second continuous-loss audit, a model-accuracy test, or an end-to-end phone test. The tool ignores preparation traffic intentionally so waiting does not overflow the recording queue.

Eight automated tests passed here, including simulated two-glove capture/save/disconnect and failure cleanup. Physical Bluetooth operation must still be checked on your laptop.

## Next checkpoint: finalize the recording protocol
Complete the separate gesture-protocol draft in this add-on, including confirmation of how the game counts CHOP, WASH interruption behaviour, and the intended pot/throw motions. AI detects gestures; game logic decides spatial validity, object ownership and progress. Per-hand confirmation/event handling remains to implement; a confidence filter alone does not prevent transition predictions or duplicates.

If final sewing/casing changes sensor placement, obtain the main dataset after that mounting is stable. Small recordings before handover are useful pilot data; label their hardware/mounting version and keep them separate from the final dataset. Keep the old eleven-feature models and recordings as a baseline rather than adding a fake third sensor channel.

When rerecording: capture a full intended action, vary onset and speed within the two-second window, and reset outside it. Partial/transition windows need deliberate labels; windows from one original capture/session must not cross dataset splits. Use the same person ID for the same wearer (the earlier reviewed datasets used P02); do not blindly accept a GUI default or rename files to change metadata.

## Example next dataset, after the hardware/report review
Both hands use separate datasets and models. Start with 20 training / 3 validation / 4 test recordings per class per hand (27/class, 189/hand). This is a small pilot baseline, not proof of generalization. Use separate acquisition episodes for the splits; merely renaming folders does not make recordings independent. Fit training normalization only on train, tune on validation, and reserve test for final evaluation. Repeatedly tuning against a test set makes it a development set; a later fresh holdout is needed.

GUI settings:
- Output root: data/hand_flex3/pilot1
- Session: flex3_pilot1_train, split train, 20 recordings per gesture.
- Session: flex3_pilot1_validation, split validation, 3 per gesture.
- Session: flex3_pilot1_test, split test, 4 per gesture.
- Use these seven classes: PICKUP DROP CHOP WASH THROW POURING IDLE.
- Select the correct physical hand. The GUI adds right/ or left/ automatically.

Start GUI:
```powershell
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.record_gui
```

Do not run the commands below until the new dataset is complete and reviewed. Choose a new output/run suffix if any destination already exists; the pipeline deliberately refuses overwrites.

RIGHT:
```powershell
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline prepare --input data/hand_flex3/pilot1/right --device 2 --out data/hand_processed/right_flex3_pilot1_v1
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline train --data data/hand_processed/right_flex3_pilot1_v1 --run models/hand_right_flex3_pilot1_v1 --epochs 30 --seed 42
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline evaluate --run models/hand_right_flex3_pilot1_v1 --split validation --out reports/hand_right_flex3_pilot1_v1_validation.json
```
After model/protocol choices are fixed:
```powershell
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline evaluate --run models/hand_right_flex3_pilot1_v1 --split test --out reports/hand_right_flex3_pilot1_v1_test.json
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.live --run models/hand_right_flex3_pilot1_v1 --device 2 --address 38:18:2B:14:EA:FE --threshold 0.70
```

LEFT:
```powershell
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline prepare --input data/hand_flex3/pilot1/left --device 1 --out data/hand_processed/left_flex3_pilot1_v1
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline train --data data/hand_processed/left_flex3_pilot1_v1 --run models/hand_left_flex3_pilot1_v1 --epochs 30 --seed 42
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline evaluate --run models/hand_left_flex3_pilot1_v1 --split validation --out reports/hand_left_flex3_pilot1_v1_validation.json
```
After choices are fixed:
```powershell
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline evaluate --run models/hand_left_flex3_pilot1_v1 --split test --out reports/hand_left_flex3_pilot1_v1_test.json
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.live --run models/hand_left_flex3_pilot1_v1 --device 1 --address 38:18:2B:17:E2:E6 --threshold 0.70
```
Close the GUI/range tool before live inference. Prepared tensors are (N,50,12); the PyTorch model receives (N,12,50). Existing 11-feature checkpoints do not fit this contract. Keep model.pth with its class_names.json, config.json and normalization.npz; nested weights may still be ignored by Git.

## Whole-system integration checkpoint
The current three-flex package covers local BLE recording and CPU training/inference. The existing common v3 codec, demo.py, laptop bridge, Ultra96 ingest/worker, phone integration, and FPGA deployment have not been migrated to v4 / LCS4 / twelve features. Do not bypass their version checks to run the whole system. Agree and update the wire schema, feature order/scales, source-time windowing, per-hand result identity and event/state contract together. Verify the actual worker command and matching weights/.bit/.hwh before a new deployment claim. Confirm availability of the updated Android app before planning its live test.

## Commits and remaining tasks
After the measurements succeed, review your local diff and commit the hardware-format work and the range diagnostic separately when possible. Suggested diagnostic commit: feat: add guided three-flex sensor range checks. Do not commit the hardware-check CSVs as training data.

Remaining checklist: sensor ranges and mapping; battery and sustained dual-hand BLE audit; final gesture protocol; final-hardware data and per-hand models; postprocessing; coordinated end-to-end app/Ultra96/FPGA migration; weekly integration entries; Tuesday meeting arrangement. Glove handover is already arranged; do not redo it here. No messages or external documents were sent/edited by this add-on.
