# RIGHT v3 — revised THROW, revised DROP preserved

The user confirmed the new THROW person ID was accidentally labelled P01.
This reviewed copy changes P01 to P02 in all 27 new THROW sidecars, matching
filenames and session markers. Sensor CSV bytes and their SHA-256 hashes are
unchanged. Original uploaded ZIPs are not modified. The correction manifest maps
all source files to the reviewed paths.

New THROW: 20 train, 3 validation, 4 test. All source split assignments were correct.
101-102 samples per recording; source duration 2.000-2.020 seconds; largest source
interval 22 ms. Checksums, packet identities, increasing sequences, source timing
and finite feature checks passed. Combined preprocessing also rejects duplicate
identities, identical model inputs and session crossing between dataset splits.
These checks do not independently verify the intended physical gesture or calibration.

Combined data: replace all 27 v2 THROW trials with these 27 new THROW trials.
Retain revised DROP from v2 and the other five classes, unchanged.
189 recordings, 140 train / 21 validation / 28 test, 20/3/4 for each class.
Verified preprocessing output: X=(189,50,11). No real-data training is run here.

## VS Code PowerShell, repository root

Save right-throw-v3-reviewed.zip into Downloads. Extract into the repository:

```powershell
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\right-throw-v3-reviewed.zip" -DestinationPath . -Force
```

Prepare:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline prepare --input data/hand_raw_reviewed/right_throw_v3 --device 2 --out data/hand_processed/right_20261023_v3
```

Train the full seven-class model:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline train --data data/hand_processed/right_20261023_v3 --run models/hand_right_20261023_v3 --epochs 30 --seed 42
```

Evaluate validation first:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline evaluate --run models/hand_right_20261023_v3 --split validation --out reports/hand_right_20261023_v3_validation.json
```

Send the training output and validation report. Existing v1/v2 datasets/models
remain intact. Use only the new reviewed root as input, not a parent directory
containing several dataset versions.

After inspecting validation, run the regression test:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline evaluate --run models/hand_right_20261023_v3 --split test --out reports/hand_right_20261023_v3_test.json
```

Only THROW test recordings are new in this version; the other classes' test
recordings were already evaluated for v2. Treat this as a development/regression
comparison, not a wholly new final holdout. Collect a fresh full holdout later
if these results guide further changes. The altered THROW validation set also
means v2/v3 validation scores are not a controlled same-input comparison.

Then test real-time CHOP/THROW behavior, including partial windows and transitions:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.live --device 2 --run models/hand_right_20261023_v3 --threshold 0.70 --interval 0.20
```

Close other BLE clients before live use. Keep the existing >0.70 output-filter
update in src/hand_ai/live.py. This ZIP is data/documentation only; it does not
replace live inference or firmware. Laptop training requires no bit/hwh changes.
If an output directory/report exists, choose a fresh versioned output name rather
than overwrite prior results.
