# Revised RIGHT DROP dataset — review and next commands

Source: drop_v2.zip uploaded on 2026-10-23.
All 27 new CSVs have matching JSON sidecars and correct hand/person/gesture IDs.
Each recording has 101-102 samples, duration 2.000-2.019 seconds, largest source
interval 22 ms. No observed sequence gaps, checksum changes, duplicate model inputs,
or filename identity mismatches. Features are finite. Values alone do not establish
that the intended physical gesture was performed or validate sensor calibration.

Found issue: all three source session markers and all 27 trial JSONs used split
train. The agreed mapping is drop_v2_train -> train (20), drop_v2_validation ->
validation (3), drop_v2_test -> test (4). Seven per-trial split fields and the two
non-training session identities are corrected in this separate reviewed copy.
Sensor CSV bytes are unchanged; their recorded SHA-256 checksums still match.

This bundle combines the six original non-DROP classes with all 27 revised DROP
recordings, excluding all 27 old DROP recordings. Combined count: 189, with
140 training / 21 validation / 28 test. Preprocessing was verified: X=(189,50,11).
No model training or test predictions were performed in the authoring environment.
Original uploads and the v1 model/dataset are untouched.

Do NOT preprocess data/hand_pilot/right wholesale: the screenshot shows both
drop_v2 and separate sibling session folders, plus the old recordings. A parent
scan could include both copies and old DROP. Use only the reviewed root below.
The split field is chosen in the GUI separately from the session name. For future
recordings always select both. This correction follows the explicit agreed session
mapping; it does not move any samples randomly or select them based on predictions.

## VS Code PowerShell — repository root

Save right-drop-v2-reviewed.zip in Downloads and extract:

```powershell
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\right-drop-v2-reviewed.zip" -DestinationPath . -Force
```

Prepare the v2 dataset:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline prepare --input data/hand_raw_reviewed/right_drop_v2 --device 2 --out data/hand_processed/right_20261023_v2
```

Train all seven classes with a NEW run folder:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline train --data data/hand_processed/right_20261023_v2 --run models/hand_right_20261023_v2 --epochs 30 --seed 42
```

Evaluate validation only:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline evaluate --run models/hand_right_20261023_v2 --split validation --out reports/hand_right_20261023_v2_validation.json
```

Send the training output and validation JSON. Compare v2 DROP recall and IDLE false
activations against v1, where DROP recall was 1/3 and overall validation 19/21.
DROP validation was re-recorded under a changed gesture definition, so the v1/v2
validation comparison is informative but not a controlled same-input comparison.
Three validation trials per class remains a small sample. Leave the test set
unscored until model/protocol choices are fixed. No firmware or bit/hwh update is
needed for this laptop experiment. If an output already exists, choose a fresh
versioned output name rather than overwriting a prior dataset/run/report.
