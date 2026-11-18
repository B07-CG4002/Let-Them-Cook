# Independent-hand AI v1 — RIGHT first

Seven labels: PICKUP, DROP, CHOP, WASH, THROW, POURING, IDLE.
Input: physical sensor units in ax,ay,az,gx,gy,gz,yaw,pitch,roll,flex1,flex2 order.
Each trial covers 2 seconds of source uptime, resampled to 50 positions.
Model input: batch x 11 x 50; output: seven logits.
This package adds src/hand_ai; it reuses src/model.py and the current common/laptop BLE modules.
It does not replace your firmware, paired-hand scripts, trained models, or FPGA deployment.

## 1. Install into the repository

Use Windows PowerShell in VS Code. Save the ZIP to Downloads, then run:

```powershell
Set-Location "C:\Users\ooiwe\OneDrive\Documents\CG4002\Let_Them_Cook"
git branch --show-current
git status --short
```

If AI-independent-hand-pipeline exists, switch to it. If you are still on the
previous branch with your reviewed BLE commit saved, create the new branch:

```powershell
git switch -c AI-independent-hand-pipeline
```

Do not run that creation command again if the branch already exists.
Extract the ZIP at the repository root (not inside src):

```powershell
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\independent-hand-ai-v1.zip" -DestinationPath . -Force
git status --short
```

New paths: src/hand_ai, tests/test_hand_ai.py, requirements_hand_ai.txt,
HAND_AI_GUIDE.md, HAND_AI_VALIDATION.txt. No firmware rebuild needed.

Check dependencies; activation is unnecessary:

```powershell
@'
import importlib
for name in ('bleak','numpy','pandas','torch','sklearn'):
    try:
        module = importlib.import_module(name)
        print(name, 'OK', getattr(module, '__version__', 'version not exposed'))
    except ImportError as error:
        print(name, 'MISSING:', error)
'@ | .\.venv\Scripts\python.exe -
```

Only if modules are missing:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements_hand_ai.txt
```

## 2. Run the tests before real recording

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_hand_ai.py -v
.\.venv\Scripts\python.exe -m src.hand_ai.record --help
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline --help
.\.venv\Scripts\python.exe -m src.hand_ai.live --help
```

Expect 11 passing tests when PyTorch is installed. One test prepares synthetic
recordings, trains for two epochs, evaluates, and loads predictions. Its accuracy
is not evidence of gesture accuracy. A skipped PyTorch test is not a training pass.

## 3. Record a pilot FIRST

RIGHT must be powered. BLE is used even with USB attached. Close other BLE
programs connected to RIGHT; a serial monitor may stay open for diagnostics.
If the monitor resets the glove, let calibration complete while it is still.

Run this in an interactive PowerShell terminal, not through a piped Python snippet:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.record --device 2 --person P01 --session pilot1 --split train --gesture PICKUP --trials 3 --out data/hand_pilot/right/pilot1
```

For each trial: press Enter when ready, wait for GO, then perform one gesture over
the two-second window. Wait for STOP before resetting your hand. IDLE trials should
include relaxed rest and ordinary non-action movements, not just one rigid pose.
The recorder prints sample count and channel ranges. Confirm that rotations and
finger bending change the expected channels. It warns about int16 saturation;
resolve repeated clipping before bulk collection. These checks do not automatically
prove calibration or correct gesture labels.

Every trial creates a CSV and matching JSON; keep them together. The output folder
has a session.json marker. To append, choose unused --start numbers. Files are
never overwritten. A quality error stops the command; completed files remain.
The pilot folder is separate from the real dataset and must not be included in it.

## 4. Define and collect the real dataset

Use one consistent physical definition for each gesture before bulk recording.
For PICKUP/DROP/THROW/POURING, do one deliberate action per trial. For CHOP/WASH,
use the agreed repeated motion during the window. PICKUP denotes its motion, not
indefinite holding: if gameplay needs a persistent held state, handle that as a
game state separately. Avoid unsafe throwing; imitate the movement without an object.

Do not automatically mix earlier differently performed gestures into this dataset.

Target per class: train 30 / validation 4 / test 6 = 40 (75/10/15).
Seven classes give 210 / 28 / 42 = 280 recordings.
Training is divided across two sessions below; every session belongs to one split.
Use a real break and re-wear/recalibration between sessions. Session names alone
are not evidence of independent sessions. Person IDs should stay consistent.

Training session trainA: 15 per class:

```powershell
$gestures = @('PICKUP','DROP','CHOP','WASH','THROW','POURING','IDLE')
foreach ($gesture in $gestures) {
    .\.venv\Scripts\python.exe -m src.hand_ai.record --device 2 --person P01 --session trainA --split train --gesture $gesture --trials 15 --out data/hand_raw/right/trainA
    if ($LASTEXITCODE -ne 0) { throw "Recording failed for $gesture. Inspect completed files before resuming." }
}
```

Take a break/re-wear/recalibrate. Training session trainB: another 15 per class:

```powershell
$gestures = @('PICKUP','DROP','CHOP','WASH','THROW','POURING','IDLE')
foreach ($gesture in $gestures) {
    .\.venv\Scripts\python.exe -m src.hand_ai.record --device 2 --person P01 --session trainB --split train --gesture $gesture --trials 15 --out data/hand_raw/right/trainB
    if ($LASTEXITCODE -ne 0) { throw "Recording failed for $gesture. Inspect completed files before resuming." }
}
```

In a separate setup session, validation: four per class:

```powershell
$gestures = @('PICKUP','DROP','CHOP','WASH','THROW','POURING','IDLE')
foreach ($gesture in $gestures) {
    .\.venv\Scripts\python.exe -m src.hand_ai.record --device 2 --person P01 --session valA --split validation --gesture $gesture --trials 4 --out data/hand_raw/right/valA
    if ($LASTEXITCODE -ne 0) { throw "Recording failed for $gesture. Inspect completed files before resuming." }
}
```

In another setup session, test: six per class:

```powershell
$gestures = @('PICKUP','DROP','CHOP','WASH','THROW','POURING','IDLE')
foreach ($gesture in $gestures) {
    .\.venv\Scripts\python.exe -m src.hand_ai.record --device 2 --person P01 --session testA --split test --gesture $gesture --trials 6 --out data/hand_raw/right/testA
    if ($LASTEXITCODE -ne 0) { throw "Recording failed for $gesture. Inspect completed files before resuming." }
}
```

Do not tune using test predictions. This is single-person evaluation, not evidence
of generalization to other users. Four validation recordings per class is a small,
noisy estimate. Increase data later while preserving split/session isolation.

To resume an interrupted collection, run one class with --start FIRST_UNUSED_TRIAL
and --trials REMAINING_COUNT. For example, after successfully saving PICKUP 1..7:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.record --device 2 --person P01 --session trainA --split train --gesture PICKUP --start 8 --trials 8 --out data/hand_raw/right/trainA
```

Check the last saved CSV/JSON pair before resuming. If there is an orphan CSV after
an interrupted disk write, inspect it and move it outside the dataset before retrying.

## 5. Prepare

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline prepare --input data/hand_raw/right --device 2 --out data/hand_processed/right_v1
```

Expected X shape: (280,50,11). It checks exact per-class 75/10/15 counts, source
sequence/timing, no mixed hands, session isolation, CSV checksums and identical
resampled inputs. It refuses an existing output directory. Use a new versioned
output directory after changing data; do not overwrite a dataset tied to a model.
All CSVs below the input root are considered: keep pilot/unrelated CSVs elsewhere.
A constant or stuck sensor can still produce valid-shaped data: review pilot values.

## 6. Train; inspect validation before touching test

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline train --data data/hand_processed/right_v1 --run models/hand_right_v1 --epochs 30 --seed 42
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline evaluate --run models/hand_right_v1 --split validation --out reports/hand_right_v1_validation.json
```

Uses the existing GestureCNN(11 features,7 classes), Adam learning rate 0.001,
batch size 32, best validation accuracy with validation loss as tie-breaker.
Feature normalization uses training data only and is saved in normalization.npz.
Artifacts include model.pth, config, class order, history, dataset hashes and the
recording split manifest. Run folders must be new. Keep all artifacts together.
CPU training is intentional for this baseline; it does not imply FPGA execution.

## 7. Untouched test evaluation

Once the model/protocol/threshold choices are fixed using training/validation:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline evaluate --run models/hand_right_v1 --split test --out reports/hand_right_v1_test.json
```

Reports accuracy, per-class metrics, confusion matrix and probabilities per trial.
Inspect IDLE false activations and action confusions, not accuracy alone.
If test results guide more development, collect a new untouched holdout for final
reporting. The command intentionally refuses to overwrite a prior report.
If files move between machines, --data can locate the same hashed prepared dataset.

## 8. Laptop live BLE inference

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.live --device 2 --run models/hand_right_v1 --threshold 0.70 --interval 0.20
```

0.70 is provisional, not a validated threshold. Use validation probabilities and
live development trials to choose it before final testing. Output identifies the
hand, boot, sequence, confidence, inference time and sample age. LOW_CONFIDENCE is
a diagnostic status, not an IDLE prediction. Only VALID outputs should become
eligible game events in a later integration worker. This CLI prints diagnostics
only; it sends no commands to Ultra96 or AR.

A two-second rolling window initially needs two seconds to fill. inference_ms
excludes the window and human reaction time; it is not end-to-end response latency.
Measure actual action onset to first correct output. The model sees continuously
sliding windows in live use, while recordings are cued trials; test transitions,
ordinary activity and long IDLE periods. If this fails, collect representative
continuous/background examples and revise the protocol before claiming reliability.
Sequence/source-time gaps reset the live window. Persistent malformed packets or
queue drops stop the run rather than quietly continuing on corrupted data.

## 9. Commit checkpoints (inspect before committing; no automatic push)

After tests pass, commit code separately from recordings/model results:

```powershell
git add src/hand_ai tests/test_hand_ai.py requirements_hand_ai.txt HAND_AI_GUIDE.md HAND_AI_VALIDATION.txt
git diff --cached --stat
git diff --cached --check
git commit -m "feat: add independent-hand BLE recording and AI pipeline"
```

Inspect git status before staging recordings; your .gitignore may exclude data or
weights. Do not force-add everything. Keep dataset/model storage decisions explicit.
Suggested later commits:
- data: add standardized right-hand gesture recordings
- feat: train seven-class right-hand gesture model
- test: document right-hand holdout and live inference results

When you explicitly choose to push:

```powershell
git push -u origin AI-independent-hand-pipeline
```

Suggested PR title: feat: add independent-hand gesture training and BLE inference
Description: Add a seven-class, 11-feature workflow for individual gloves. Record
BLE samples with source timestamps and session identities, enforce 75/10/15
session-separated splits, train/evaluate with saved normalization, and provide
laptop live diagnostics. Include only validation results actually obtained locally;
do not claim physical or training validation from synthetic tests.

## 10. Ultra96 / FPGA / LEFT — subsequent checkpoints

This kit completes the laptop recording-to-live-software experiment. It does not
modify src.socket_worker_full or the full-CNN FPGA implementation. Do not load
these weights into the previous 22-input/eight-output deployment. Input dimensions,
output class order and normalization have changed. Board deployment requires an
updated worker and hardware/backend compatibility review, then software-vs-board
comparison using the exact new weights. Inspect the current board command first.

When LEFT returns: flash/test its BLE fix, record LEFT data using --device 1 and a
separate raw root, prepare/train a separate run, then compare cross-hand/shared
weights explicitly. The live loader currently rejects using a model on another
hand. Two laptop CLI processes can eventually run independently, but simultaneous
BLE behavior still needs testing. A later board worker should route by device_id,
keep independent windows/states, suppress low-confidence game events and define
repeat/debounce behavior. Repeated VALID windows are not automatically distinct
actions. Do not report dual-hand/AR success until tested.
