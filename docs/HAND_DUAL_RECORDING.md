# Dual recording and temporary RIGHT-index fallback

Reviewed against main 521c7e5302e257de24ba6825218ca98e7abfb9c0, 28 Oct 2026.
This adds new AI files only. It does not overwrite your teammate's PR #37 changes.

## Relevant Git update

PR #37 is merged: https://github.com/B07-CG4002/Let-Them-Cook/pull/37
It adds explicit v4 mode to demo/dual-bridge/deployment launchers, a starved-link
watchdog, hand12 staging and worker result delivery/reconnection. The commit
reports synthetic Ultra96 testing with 72,016 accepted samples, 3,472 results,
and no refused results. These are its reported integration results, not final
gesture accuracy on the repaired gloves.

Important: GESTURE_RESULT stays version 3 for existing phone parsers. Pulses,
state_start, refresh and idle are forwarded in legacy result form. State_end
and reset remain local logs. Full structured event/lease delivery is therefore
still distinct from that working compatibility path. Earlier AI handover notes
about missing launcher propagation and an observer-only worker predate PR #37.

## Install and first check

Extract this ZIP into Downloads, not over the repository. Use the installer in
START_HERE.txt. It checks source prerequisites and refuses different existing
target files. No existing code is replaced, no firmware uploaded and no service
restarted. Pull the team's latest main using your normal workflow first; keep
your seven untracked experimental event files separate.

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_hand_dual_recording.py" -v
if ($LASTEXITCODE -ne 0) { throw "Dual recording tests failed." }
```

Expected: 12 tests, zero skips on your PyTorch training environment. Preparation
here passed 11 tests; the real PyTorch training/export test was skipped because
PyTorch is absent here. Do not treat a skip as verification of training/export.
Actual Windows GUI/BLE and hard-link behavior still need the pilot below.

## Record both hands together

Close the old recorder/other BLE clients. Run:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.record_both_gui
```

- Connect both gloves. Both must connect and continuously stream before saving.
- Start with default diagnostic output `data/hand_flex3_checks/dual_pilot`, P01,
  session `P01_dual_pilot1`, split train, count 3. This is outside final raw data.
- Select LEFT and RIGHT labels separately. For the first connection/save pilot,
  choose IDLE for both and record three pairs; then try WASH for both.
- One countdown cues both. Source clocks are independent: shared GO does not
  claim synchronized or sample-aligned sensors. Each hand is resampled separately.
- Record complete gestures inside 2 seconds. Do not label an inactive hand with
  the other hand's action. Retain natural timing/style; don't force clipped actions.
- Numbering resumes independently per hand/gesture; count means additional pairs.
- If either capture fails, neither hand's current trial is committed. Cancel
  keeps connections and completed pairs; disconnect closes both connections.
- Both CSVs retain all twelve raw channels, including the faulty RIGHT flex1.
  Sidecars retain native metadata/hashes plus pair ID and the other hand's label.
- Pair files use a shared staging directory and exclusive hard links; a reported
  commit failure rolls back newly created files. A power/process crash in the
  middle of filesystem operations is not a guaranteed atomic multi-file commit;
  audit incomplete pairs before selecting a final dataset. Use local NTFS storage
  if OneDrive/another filesystem refuses hard links.

After the pilot, use your collection plan's real root/person/sessions/splits and
counts. Native prepare enforces per-class pooled rounded ratios. Arbitrary extra
IDLE trials from asymmetric actions may exceed that plan; keep diagnostic and
continuous integration recordings separate from the selected native dataset.

## Why the temporary RIGHT model ignores index

Known soldering faults are unreliable training input. The fallback explicitly
removes RIGHT flex1 (feature index 9 / GPIO34) from model decisions while leaving
the raw recordings intact. IMU + middle + thumb remain available. This does not
repair hardware, create missing information or guarantee gesture accuracy.
Physical electrical faults that affect other channels/power/streaming still
require hardware repair. Fixing the solder connection remains the final goal.

The tool trains the existing 12-input CNN with normalized index input masked to
zero. At export it zeros that channel's first-convolution weights and compares
masked/folded predictions on validation inputs with changed index values. Other
features, class order and training-only normalizers remain unchanged. Generic
native CPU/FPGA inference then ignores the index automatically through weights;
no separate comms mask or input dimension change is required. Keep finite raw
channel values; packet/NaN/source health checks are still enforced.

The lab checkpoint uses a custom profile. Do not load it directly with other
lab commands; first export using this tool. The exported native model supports
the normal replay/evaluate/HLS bundle tools. The final LEFT+RIGHT bundle still
needs selected weights and numeric/board validation. No new bit/HWH is needed
solely for this zero-weight channel if architecture/arithmetic stay unchanged.

## Training after a reviewed, complete collection

Prepare each hand separately with `src.hand_ai_flex3.pipeline prepare`, as in
HAND_RECORDING_READY.md. Use new output folders. The following assumes these
prepared datasets exist and are complete, not just the three-pair pilot:

```powershell
.\.venv\Scripts\python.exe -m tools.hand_index_fallback train `
    --data data/hand_processed/right_fitted_v1 `
    --out .comms-local/ai_lab/right_no_index_v1 --epochs 30 --seeds 42 73 101
if ($LASTEXITCODE -ne 0) { throw "RIGHT fallback training failed." }
```

Read comparison.json and choose the seed using validation macro-F1 (then loss),
not test scores. Example if seed 42 is selected:

```powershell
.\.venv\Scripts\python.exe -m tools.hand_index_fallback export `
    --run .comms-local/ai_lab/right_no_index_v1/seed_42 `
    --out models/hand_right_no_index_v1
if ($LASTEXITCODE -ne 0) { throw "RIGHT fallback export failed." }

.\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline evaluate `
    --run models/hand_right_no_index_v1 --split validation `
    --out reports/hand_right_no_index_v1_validation.json
if ($LASTEXITCODE -ne 0) { throw "RIGHT validation failed." }
```

LEFT uses ordinary twelve-feature training; no LEFT feature is disabled. Train
with `src.hand_ai_flex3.pipeline train` or the existing lab all12 workflow. Both
models remain independent, even when their raw trials were captured together.
Recordings sharing a pair/donning session must stay in the same split. Fresh
sessions are actual collection conditions, not simply renamed folders.

Then use continuous validation replay to inspect resets, duplicate actions,
transition errors and delay; reserve a fresh held-out test for final evaluation.
After hardware repair, train a new all12 RIGHT run, retain the fallback with its
provenance, and compare on validation. Do not silently reactivate the index in
this fallback checkpoint or overwrite old raw recordings/model runs.

## Commit scope after Windows tests and GUI pilot pass

Four new files only: src/hand_ai_flex3/record_both_gui.py,
tools/hand_index_fallback.py, tests/test_hand_dual_recording.py,
docs/HAND_DUAL_RECORDING.md. Do not stage generated data/models, downloaded kit
files or the seven experimental event-transport files.

Commit: feat(ai): add dual-glove recorder and right-index fallback
PR title: Add simultaneous glove recording and temporary right-index exclusion
PR description: Adds a shared-cue dual-BLE recorder with separate native hand
trials and pair provenance. Adds RIGHT-only masked training and native export
that folds the faulty index exclusion into conv1 weights. Preserves all raw
channels and existing comms/FPGA interfaces. Include actual Windows test and
physical recorder pilot results; distinguish synthetic checks from accuracy.
