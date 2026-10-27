# Next recording and training checkpoint

Prepared 27 October 2026 against merged main commit
`0acee145cbe4e5e29a0f58addd8841a2926ee211` (PR #35).

The native twelve-feature recording/training pipeline, hand runtime and
board-verified hand12 scaffold are merged. Final sensor fitting, real twelve-
feature model accuracy and full BLE-to-game integration remain separate work.
The shared board service was restored to its older paired model after testing.
Checking in the new overlay did not deploy it as the live service.

## Start here when you return

This package adds only `tools/hand_collection_plan.py`, its tests and this guide.
It changes no recorder, model, firmware, comms code or FPGA files. No gloves,
Vitis process or board access are needed for installation and planning.

Run from the repository root after installing:

```powershell
.\.venv\Scripts\python.exe -m unittest discover `
    -s tests -p "test_hand_collection_plan.py" -v

if ($LASTEXITCODE -ne 0) { throw "Collection planner tests failed." }

# Use actual wearer IDs. Add P02 P03 etc only for people who will participate.
.\.venv\Scripts\python.exe -m tools.hand_collection_plan plan `
    --people P01 `
    --revision fitted_v1 `
    --raw-root data/hand_raw_flex3/fitted_v1 `
    --out .comms-local/recording_plan/fitted_v1.json

if ($LASTEXITCODE -ne 0) { throw "Collection planning failed." }
```

Outputs are new files only. To change a plan, choose a new plan filename and
review the implications for already-collected data. Do not silently relabel
existing sessions. The plan creates no raw directories and starts no recordings.
Relative raw roots resolve from the repository directory supplied to `progress`.

## A group-collection issue caught before recording

Native prepare currently enforces largest-remainder 75/10/15 rounding across
the **whole selected hand dataset for each gesture**, rather than independently
for each person. These are compatible cohort totals per class/per hand:

| Wearers | Train | Validation | Test | Total |
| --- | ---: | ---: | ---: | ---: |
| 1 | 20 | 3 | 4 | 27 |
| 2 | 41 | 5 | 8 | 54 |
| 3 | 61 | 8 | 12 | 81 |
| 6 | 122 | 16 | 24 | 162 |

Blindly recording 20/3/4 for each of two wearers gives 40/6/8; native prepare
refuses that total. The new planner distributes compatible cohort counts over
the actual wearers without changing the existing preparer or moving recordings.
Every generated session prints its per-gesture target. This allocation is a
compatibility choice, not a scientific requirement for exact split percentages.

Default volume is 378 trials per wearer across both hands. For six people this
is 2,268 trials, at least 3.15 hours just for two-second recordings plus the
GUI's three-second preparation, before pauses, refitting or retries. Decide the
collection scope before promising the whole group in a short meeting. A smaller
pilot is sensible for checking fit; it need not be training-ready yet.

The default plan evaluates the same listed people in genuinely different
sessions. It does not establish accuracy on an unseen wearer. Do not randomize
overlapping windows to simulate independent people or sessions.

## When the gloves are available

1. Settle fitting and IMU mounting first. Photograph orientation, finger placement
   and hardware revision. LEFT must be physical LEFT/device 1; RIGHT must be
   physical RIGHT/device 2. Index=flex1/GPIO34, middle=flex2/GPIO35,
   thumb=flex3/GPIO39. Verify this with individual finger movements.
2. Close competing BLE clients, connect over Bluetooth, and check both hands'
   flex response, IMU response, source continuity and actual identity. COM4 is
   a USB port, not a BLE address. ADC/4095 is not a calibrated bend percentage.
3. Record a small pilot outside the final selected root. Inspect repeatability,
   sensor ranges and clipping, and reject obvious fit/wiring problems before
   starting the full plan. A file audit cannot verify physical sensor placement.
4. Use the unchanged recorder:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.record_gui
```

For the one-person default plan, enter these GUI values. Every row covers all
seven gestures separately; the batch count is per gesture.

| GUI setting | Value |
| --- | --- |
| Output root | `data/hand_raw_flex3/fitted_v1` |
| Person | `P01` (actual wearer) |
| Hand | LEFT or RIGHT, verified physically |
| Train session / split / count | `P01_fitted_v1_train_s1` / train / 20 |
| Validation session / split / count | `P01_fitted_v1_validation_s1` / validation / 3 |
| Test session / split / count | `P01_fitted_v1_test_s1` / test / 4 |

For a group plan, use the printed per-person counts instead of this one-person
table. Session names are shared across the two hand folders but not across
splits. They must represent separate collection/donning conditions; changing
a string while continuing the same collection does not establish independence.

Fit a complete natural action into each two-second trial, varying onset without
clipping the actual gesture. Keep supported THROW style consistent with the
game's agreed definition. Include genuine holding/resting postures in IDLE;
arbitrary transitional movement is not automatically IDLE. Record separate
continuous/video validation with natural preparations, returns, pauses and
different concurrent LEFT/RIGHT actions.

After a disconnect, reconnect and keep the same identity/session only if the
collection condition is the same. Batch count means **additional trials**.
The recorder starts at the highest used trial index + 1; it does not replace
bad trials, fill missing numbers or stop at an overall target automatically.
The new report counts valid files, not the highest filename number.

Keep bad-trial CSV+JSON backups and their reasons outside the selected raw root.
Review exclusions explicitly; do not rename another session/person's recording
to disguise it as a replacement. Number gaps are harmless. Identity/provenance
and the selected valid counts matter.

## Check progress after stopping the current recording batch

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
.\.venv\Scripts\python.exe -m tools.hand_collection_plan progress `
    --plan .comms-local/recording_plan/fitted_v1.json `
    --repo . `
    --out ".comms-local/recording_plan/progress_$stamp.json"

if ($LASTEXITCODE -ne 0) { throw "Review the reported recording errors before continuing." }
```

`incomplete` is normal during collection and exits successfully, with remaining
counts printed. `blocked` exits 2 for integrity, identity, orphan sidecar or
excess-count problems. `collection_complete` means planned counts and the file
checks passed; it does not certify action labels, fit, live accuracy or phase
completion. The report includes per-channel ranges and existing audit warnings.
No files are deleted, renamed, repaired, automatically excluded or overwritten.
Keep all notes/backups outside this explicitly selected root: every CSV inside
is treated as a candidate trial, and extra JSONs are treated as orphan sidecars.

## Prepare and train each hand after collection and manual quality review

These are native three-flex commands. Do not use the older `src.hand_ai` module
or the old eleven-feature model. Choose new output directories for each run.

```powershell
foreach ($hand in @("left", "right")) {
    $deviceId = if ($hand -eq "left") { 1 } else { 2 }
    $dataDir = "data/hand_processed/${hand}_fitted_v1"
    $runDir = "models/hand_${hand}_fitted_v1"

    .\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline prepare `
        --input "data/hand_raw_flex3/fitted_v1/$hand" `
        --device $deviceId --out $dataDir
    if ($LASTEXITCODE -ne 0) { throw "Prepare failed for $hand." }

    .\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline train `
        --data $dataDir --run $runDir --epochs 30 --seed 42
    if ($LASTEXITCODE -ne 0) { throw "Train failed for $hand." }

    .\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline evaluate `
        --run $runDir --split validation `
        --out "reports/hand_${hand}_fitted_v1_validation.json"
    if ($LASTEXITCODE -ne 0) { throw "Validation failed for $hand." }
}
```

Then use `docs/AI_LAB_START_HERE.md` for controlled feature/seed experiments,
continuous validation replay and confirmation tuning. Freeze model and output
settings on validation before looking at the fresh final test. The existing lab
supports all12 / imu_flex2 / imu9 comparisons, seeds 42/73/101, precision screens
and timing reports; those tools do not require another implementation now.

## Where to spend optimization effort

The physical synthetic hand12 test measured about 1.194 ms per warm call.
At five decisions per second for each hand, ten such calls sum to about
11.94 ms per second. This is an arithmetic budget estimate for that measured
call scope, not measured whole-system utilization or real-model accuracy.

Three consecutive decisions at 0.2-second spacing take about 0.4 seconds from
the first qualifying prediction to confirmation. The initial rolling history
also needs filling. A faster FPGA cannot eliminate this decision delay or make
misclassified transitions correct. Measure these separately:

| Evidence | What it diagnoses |
| --- | --- |
| Source sequence/uptime and callback age | Sensor continuity, batching and delivery |
| Exact same saved window on CPU/fixed/FPGA | Preprocessing, numeric or implementation disagreement |
| Video onset to first correct raw prediction | Window/training behavior |
| First qualifying prediction to acceptance | Confirmation/rearm settings |
| Accepted AI event to game reaction | Downstream delivery/game behavior |

Priority after fitted-glove collection: validate complete actions and transitions;
compare useful thumb information with a controlled ablation; tune confirmation
on validation; benchmark selected real weights; only then narrow precision or
add hardware lanes if measurements justify the cost. Keep model weights,
configuration, normalizer, evaluation reports and raw/provenance backups together.

Runtime-loaded new weights can reuse the implemented hand12 overlay when the
architecture, parameter layout and arithmetic remain identical. Export the
actual selected LEFT+RIGHT native runs with `tools.hand_hls_prepare`, run their
numeric/C-simulation checks, and coordinate a board verification/deployment slot.
Do not rebuild merely because training produced different weights. A changed
numeric format, architecture or interface does require matching implementation
and verification. Never load synthetic fixture weights as the final live model.

## Research checked for this checkpoint

- AMD UG1399 **2026.1**, released 11 September 2026, explains that memory-port
  contention can limit pipelining; array partitioning/reshaping are remedies
  when the access pattern warrants them. More unrolled arithmetic alone does
  not establish lower latency. This supports profiling before more lanes.
  https://docs.amd.com/r/en-US/ug1399-vitis-hls/Array-Accesses-and-Performance
- Scikit-learn's official common-pitfalls guidance requires consistent transforms
  at inference and fitting preprocessing on training data. Keep each hand's
  selected normalizer attached to its weights and avoid split leakage.
  https://scikit-learn.org/stable/common_pitfalls.html
- PyTorch's reproducibility guidance discusses seeds/deterministic algorithms
  and limits across releases/platforms. Multiple seeds show training variation;
  they do not substitute for independent sessions or new wearers.
  https://docs.pytorch.org/docs/stable/notes/randomness.html

The split-rounding finding comes from this repository's current
`src/hand_ai_flex3/core.py`, not from a research requirement. This delivery's
13 tests passed on Linux/Python 3.12 with NumPy, including a complete 378-trial
synthetic file fixture. Windows, actual recordings and physical sensors were
not executed by this new planner test run. Existing board results are prior
project evidence, not newly rerun measurements.
