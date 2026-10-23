# RIGHT dataset: preprocessing and training

Use VS Code PowerShell at the repository root. Download the ZIP into Downloads.
This update contains two replacement code/test files and a reviewed data copy.
The data copy is placed at data/hand_raw_reviewed/right; original folders stay as-is.
Do not copy or merge the reviewed CSVs back into the original folders: this would
create duplicated recordings. Use only the reviewed root below for this run.

```powershell
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\hand-ai-right-dataset-update.zip" -DestinationPath . -Force
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_hand_ai*.py" -v
```

Expected: 32 tests and OK with your PyTorch environment. No hardware is needed.
The printed synthetic test accuracy is unrelated to these real recordings.

Prepare (choose a new output name if this directory already exists):

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline prepare --input data/hand_raw_reviewed/right --device 2 --out data/hand_processed/right_20261023_v1
```

Expected counts: each label train=20, validation=3, test=4.
Expected shape: (189,50,11). Folder names pilot1/pilot1_validation/pilot1_test are
valid; split metadata decides the assignment. The reviewed data includes pilot2
for four CHOP training recordings to match their existing metadata.

Train:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline train --data data/hand_processed/right_20261023_v1 --run models/hand_right_20261023_v1 --epochs 30 --seed 42
```

Evaluate validation only first:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline evaluate --run models/hand_right_20261023_v1 --split validation --out reports/hand_right_20261023_v1_validation.json
```

Send training output and validation report before proceeding. Three validation
recordings per class is a small sample: review per-class confusions, not just the
headline accuracy. Use only training/validation to choose model changes and
thresholds. The new run stores normalization fitted on training data only.

Later, when model choices are fixed, evaluate the untouched test set:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.pipeline evaluate --run models/hand_right_20261023_v1 --split test --out reports/hand_right_20261023_v1_test.json
```

Live RIGHT laptop inference after evaluation:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai.live --device 2 --run models/hand_right_20261023_v1 --threshold 0.70 --interval 0.20
```

Threshold 0.70 is provisional. Select it with validation/development data before
final testing. Keep low-confidence output distinct from confident IDLE. No bit/hwh
change is needed for laptop training. These weights are not compatible by default
with the previous paired-hand FPGA deployment.

Suggested separate code commit after successful tests:

```powershell
git add src/hand_ai/core.py tests/test_hand_ai.py
git diff --cached --stat
git diff --cached --check
git commit -m "fix: accept rounded gesture dataset splits"
```

Dataset/model commits can follow after review. Inspect git status and .gitignore;
do not force-add everything. No push or PR is performed by these instructions.
