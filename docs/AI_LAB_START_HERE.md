# AI improvement toolkit: start here

Wen Ree — all commands below run in **Windows PowerShell at the repository root**
unless the section explicitly says Ultra96/Linux. Keep using
`.\.venv\Scripts\python.exe`. No activation is needed.

This package adds AI-only modules, tests and guides. It changes no existing
recorder, firmware, comms service, phone code, model weights or FPGA artifacts.
The seven uncommitted experimental event-transport files are separate. Leave
them uncommitted for comms review; the previously reported event-transition test
is still unresolved. This toolkit does not import those experimental modules.

## 1. First actions after waking up

1. Download **hand-ai-improvements-v2.zip** to Downloads.
2. Stay on `AI-fpga-independent-hand` for installation/testing. The installer
   checks reviewed runtime prerequisites and all targets before writing. It
   refuses incompatible existing edits; there is no force option.
3. Run:

```powershell
Set-Location "C:\Users\ooiwe\OneDrive\Documents\CG4002\Let_Them_Cook"

Expand-Archive `
    -LiteralPath "$env:USERPROFILE\Downloads\hand-ai-improvements-v2.zip" `
    -DestinationPath "$env:USERPROFILE\Downloads\hand-ai-improvements-v2" -Force

.\.venv\Scripts\python.exe `
    "$env:USERPROFILE\Downloads\hand-ai-improvements-v2\install_ai_lab.py" --repo .

if ($LASTEXITCODE -ne 0) { throw "Installer stopped. Send the filenames/output." }

.\.venv\Scripts\python.exe -m unittest discover `
    -s tests -p "test_hand_ai_lab.py" -v

if ($LASTEXITCODE -ne 0) { throw "AI lab tests failed. Send the final output." }

.\.venv\Scripts\python.exe -m tools.hand_ai_lab demo `
    --out .comms-local/ai_lab/demo_v1

if ($LASTEXITCODE -ne 0) { throw "Synthetic demo failed." }

Start-Process ".comms-local\ai_lab\demo_v1\replay\timeline.html"
```

Choose a new output name (demo_v2, etc.) when rerunning. Every experiment/report
refuses overwrite. The demo runs virtual time without sleeping and does not
require gloves, BLE, Vitis, FPGA, PyTorch or an SSH connection. NumPy is required.
Expected demo: `status=passed`, 302 predictions, 6 pulse actions, 6 state starts
and 6 state ends. These are synthetic software checks, not classifier accuracy.

The test suite includes real PyTorch training/precision tests. In the preparation
environment PyTorch was absent, so those tests were skipped. On your existing
PyTorch environment they should run. A skipped test is not a verified result.
If PyTorch is missing, keep using the working project environment/install plan;
do not upgrade all packages just to run this toolkit.

The timeline is a standalone HTML file with no CDN/network dependency. Its
JavaScript syntax was checked here; browser rendering needs checking on your PC
because a browser executable was unavailable in the preparation environment.

## Coverage of the eight priorities

| Priority | Implemented path | Needs real data/hardware? |
| --- | --- | --- |
| 1 Continuous evaluation | import-triage, replay, timeline editor, action metrics | Real accuracy requires reviewed real recordings. |
| 2 Train/live mismatch | complete-one-shot window labels; explicit .5/1/1.5/2-second preparation and matching replay | Real continuous recordings and annotation. |
| 3 Separate three-flex models | native per-hand training workflow plus lab training/hand checks | Stable glove fitting and each hand's data. |
| 4 Dataset integrity | raw audit, hash checks, grouped splits, no duplicate windows, training-only normalization | Audit can run on existing three-flex data now. |
| 5 Confirmation | validation-only threshold/count/timeout grid using cached predictions | Reviewed validation stream for useful tuning. |
| 6 Feature ablation | imu9, imu_flex2, all12; identical splits and seeds | PyTorch and prepared data. |
| 7 Quantization | validation-only precision command, training calibration, overflow/gate checks | Trained models; new candidate HLS arithmetic still requires validation. |
| 8 Speed | CPU benchmark, two-slot alternating/cold FPGA benchmark, HLS XML summary | Physical FPGA timings require an isolated board and matching new artifacts. |

Lab models deliberately have their own contract. Feature masking retains twelve
CNN inputs; it is an accuracy ablation, **not** a smaller hardware architecture.
Do not point the existing production predictor at a lab run. Only the explicit
full-feature/two-second compatibility export described below is supported.

## 2. Inspect existing continuous triage recordings

Find available captures:

```powershell
Get-ChildItem -LiteralPath .\data -Recurse -File -Filter right_raw.csv |
    Select-Object -ExpandProperty DirectoryName
```

Set `$captureFolder` to the exact directory containing `right_raw.csv` and/or
`left_raw.csv` from that output. This is the only path selection required here.
Do not combine separate runs into one folder or fabricate missing samples.

```powershell
$captureFolder = Read-Host "Paste the full triage capture folder path"

.\.venv\Scripts\python.exe -m tools.hand_ai_lab import-triage `
    --input "$captureFolder" `
    --person P01 --session old_triage_diagnostic --split diagnostic `
    --out .comms-local/ai_lab/import_old_v1
```

The importer preserves all twelve physical-unit features, boot/sequence/uptime,
callback and consume times. It produces `capture.jsonl`, an unreviewed annotation
template, source hashes and (when present) separate cue-reference information.
It never treats a 10-second cue block as frame-level truth.

You can replay the known old RIGHT model explicitly as an eleven-feature
diagnostic. The raw capture still retains thumb/flex3. Its different sensor fit
and recording distribution limit what these old weights can establish:

```powershell
.\.venv\Scripts\python.exe -m tools.hand_ai_lab replay `
    --capture .comms-local/ai_lab/import_old_v1/capture.jsonl `
    --right-run models/hand_right_20261023_v3 --legacy11 `
    --out .comms-local/ai_lab/old_right_unscored_v1

Start-Process ".comms-local\ai_lab\old_right_unscored_v1\timeline.html"
```

If that model directory is unavailable, run the synthetic demo now and use real
replay once the folder is restored. A model must match the requested hand.
RIGHT-only replay never reports the unmodelled LEFT hand as missed actions.

## 3. Annotate observed action instances

1. Open the replay timeline alongside the video/your observed timing notes.
2. Add nonoverlapping intervals for each hand independently. Times use the
   capture's callback elapsed-time origin; align video to an identifiable cue.
3. PICKUP/DROP/THROW intervals cover the full physical action, once each.
   Ongoing CHOP/WASH/POURING intervals cover the intended sustained action.
4. Use IDLE for genuine idle, BACKGROUND for reviewed preparation/reset or
   unrelated motion, and UNKNOWN for time you cannot confidently label.
   BACKGROUND is an evaluation annotation, not a new eighth model output.
5. Tick the review checkbox and download `annotations.json`. Copy it from
   Downloads into the imported capture directory, keeping the original raw
   recordings and original annotation template.

```powershell
Copy-Item -LiteralPath "$env:USERPROFILE\Downloads\annotations.json" `
    -Destination .comms-local/ai_lab/import_old_v1/annotations_reviewed.json

.\.venv\Scripts\python.exe -m tools.hand_ai_lab replay `
    --capture .comms-local/ai_lab/import_old_v1/capture.jsonl `
    --annotations .comms-local/ai_lab/import_old_v1/annotations_reviewed.json `
    --right-run models/hand_right_20261023_v3 --legacy11 `
    --out .comms-local/ai_lab/old_right_reviewed_v1
```

This old capture is diagnostic; do not relabel it validation solely to unlock a
sweep. New captures must have their purpose assigned before tuning or inspection.

Reports distinguish pure-window classification, complete-action training-window
classification, diagnostic endpoint labels and accepted-action metrics. Unknown
and unannotated time is excluded, never silently converted to IDLE. One action
instance can match one acceptance; additional acceptances count as false
positives/duplicates. FP rate is per **reviewed hand-minute**, summed across
hands. The 0.5-second late matching tolerance is an evaluation policy, not a
course requirement; freeze it before final testing.

Offline onset-to-accept time uses recorded arrival times and deterministic
scheduling. It is not a measurement of BLE/Ultra96/phone end-to-end latency.
Callback-to-consume delays and source gaps help diagnose the saved input path;
they do not establish packet loss on a network path that was not captured.

## 4. New recordings after hardware fitting is verified

Read `AI_LAB_RECORDING_PROTOCOL.md` first. Start the existing BLE GUI:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.record_gui
```

Use separate roots such as `data/hand_raw_flex3/right_final_v1` and
`data/hand_raw_flex3/left_final_v1`. For the first pilot, each class/hand can use
20 training, 3 validation and 4 test recordings in genuinely separate sessions.
The existing trial pipeline validates this rounded ratio. Larger group/session
experiments should preserve grouping even when exact percentages differ.

Before preparing each hand, audit only its new THREE-FLEX recording folder:

```powershell
.\.venv\Scripts\python.exe -m tools.hand_ai_lab audit `
    --input data/hand_raw_flex3/right_final_v1 `
    --out .comms-local/ai_lab/right_raw_audit_v1.json

if ($LASTEXITCODE -ne 0) { throw "Recording audit found errors; inspect the report." }
```

Do not use the old 11-feature reviewed datasets as three-flex data. The audit
checks hashes, identity, timestamps, duplicate inputs and split leakage. A small
raw flex span is evidence to inspect, not a calibrated bend-angle measurement.

Native full-feature baseline (repeat for LEFT with device 1 and LEFT paths):

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline prepare `
    --input data/hand_raw_flex3/right_final_v1 --device 2 `
    --out data/hand_processed/right_flex3_final_v1

if ($LASTEXITCODE -ne 0) { throw "Prepare failed." }

.\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline train `
    --data data/hand_processed/right_flex3_final_v1 `
    --run models/hand_right_flex3_final_v1 --epochs 30 --seed 42

if ($LASTEXITCODE -ne 0) { throw "Training failed." }

.\.venv\Scripts\python.exe -m src.hand_ai_flex3.pipeline evaluate `
    --run models/hand_right_flex3_final_v1 --split validation `
    --out reports/hand_right_flex3_final_v1_validation.json
```

Keep final test evaluation for the frozen candidate. The older test recordings
already used to choose gesture protocols/models are development evidence now.

## 5. Controlled feature ablation and repeated seeds

The following runs all three feature sets over the same prepared data, three
seeds each, selecting checkpoints on validation macro-F1 then loss. Training
and output stay on the laptop. Test predictions are not evaluated:

```powershell
foreach ($featureSet in @("imu9", "imu_flex2", "all12")) {
    .\.venv\Scripts\python.exe -m tools.hand_ai_lab train `
        --data data/hand_processed/right_flex3_final_v1 `
        --features $featureSet --seeds 42 73 101 --epochs 30 `
        --out ".comms-local/ai_lab/right_ablation_v1/$featureSet"

    if ($LASTEXITCODE -ne 0) { throw "Training failed for $featureSet." }
}
```

Review each `comparison.json`, per-seed history and replay results. Mean/std over
three seeds measure training variability on this split; they are not confidence
intervals for unseen people. Optional `--noise-std 0.01` adds small standardized
training-only noise; leave it at zero for the baseline and compare on validation.
No arbitrary IMU rotation or flex reordering is introduced.

Validation evaluation of a lab run:

```powershell
.\.venv\Scripts\python.exe -m tools.hand_ai_lab evaluate `
    --run .comms-local/ai_lab/right_ablation_v1/all12/seed_42 `
    --data data/hand_processed/right_flex3_final_v1 --split validation `
    --out .comms-local/ai_lab/right_ablation_v1/all12_validation.json
```

For final test, freeze the chosen feature set/seed/window/filter first, then use
`--split test --allow-test-use` with a new output filename. Test use is recorded.

## 6. Continuous training and duration experiments

Record separate train/validation/test continuous runs with video after hardware
is stable. Import each as in section 2, with truthful person/session/split.
Review annotations independently of model predictions. Make `collection.json`
as a JSON list containing `capture` and `annotations` paths for each run. Paths
can be absolute, or relative to collection.json. There is a template in
`docs/ai_lab_collection.example.json`; replace every example path first.

```powershell
foreach ($seconds in @(0.5, 1.0, 1.5, 2.0)) {
    .\.venv\Scripts\python.exe -m tools.hand_ai_lab prepare-continuous `
        --manifest .comms-local/ai_lab/collection.json --device 2 `
        --seconds $seconds --stride 0.2 `
        --out ".comms-local/ai_lab/right_duration_v1/data_$seconds"

    if ($LASTEXITCODE -ne 0) { throw "Duration preparation failed for $seconds. Review class coverage and annotations." }

    .\.venv\Scripts\python.exe -m tools.hand_ai_lab train `
        --data ".comms-local/ai_lab/right_duration_v1/data_$seconds" `
        --features all12 --seeds 42 73 101 --epochs 30 `
        --out ".comms-local/ai_lab/right_duration_v1/models_$seconds"

    if ($LASTEXITCODE -ne 0) { throw "Duration training failed." }
}
```

Grouping is checked before window extraction. One-shot windows must contain one
complete annotated action and only reviewed non-action context around it. Ongoing
and IDLE windows must be pure. Windows spanning two different actions, gaps,
UNKNOWN or unannotated context are excluded. A one-second window cannot contain
a full 1.5-second throw; insufficient class coverage raises an error rather than
inventing partial-action labels. Larger counts of overlapping windows do not
create independent extra trials. Review counts/class balance before training.

These lab runs preserve 50 positions and 12 CNN inputs, while their declared
duration changes. Replay reads the model duration and uses matching windowing.
You cannot shorten only live inference while keeping a model trained for a
different duration and call the comparison controlled.

## 7. Replay and tune new models

For a reviewed validation capture, use the chosen native or lab RIGHT run; add
`--left-run` only when the matching LEFT model exists:

```powershell
.\.venv\Scripts\python.exe -m tools.hand_ai_lab replay `
    --capture .comms-local/ai_lab/validation_capture/capture.jsonl `
    --annotations .comms-local/ai_lab/validation_capture/annotations_reviewed.json `
    --right-run models/hand_right_flex3_final_v1 `
    --out .comms-local/ai_lab/right_validation_replay_v1

.\.venv\Scripts\python.exe -m tools.hand_ai_lab sweep `
    --replay .comms-local/ai_lab/right_validation_replay_v1 `
    --out .comms-local/ai_lab/right_confirmation_sweep_v1
```

The sweep changes only confidence/confirmation/timeout over cached predictions,
using strict `confidence > threshold`. It does not change the model, duration or
prediction interval. Test/diagnostic captures are refused for parameter selection.
Review F1, false acceptances and detection delay together. The default .6-second
timeout can compete with three changed predictions at .2-second spacing; longer
timeouts retain ongoing states longer, so evaluate both benefits and costs.

Freeze settings, then replay a fresh test capture using explicit flags, e.g.
`--threshold 0.7 --confirmations 3 --interval 0.2 --timeout 0.9` **only if those
settings were selected on validation**. This is an example, not a prescribed fix.

## 8. Quantization and speed: CPU first, hardware later

CPU inference benchmark on training inputs, excluding model load:

```powershell
.\.venv\Scripts\python.exe -m tools.hand_ai_lab benchmark-cpu `
    --run models/hand_right_flex3_final_v1 `
    --data data/hand_processed/right_flex3_final_v1 --repeats 100 `
    --out .comms-local/ai_lab/right_cpu_benchmark_v1.json

.\.venv\Scripts\python.exe -m tools.hand_ai_lab precision `
    --run models/hand_right_flex3_final_v1 `
    --data data/hand_processed/right_flex3_final_v1 `
    --activation-widths 24 20 16 --weight-widths 16 12 10 `
    --out .comms-local/ai_lab/right_precision_v1
```

The precision command also accepts lab models. It profiles TRAIN only, evaluates
VALIDATION only, compares NumPy/PyTorch, checks clipping/class/strict-.70 gate
changes and probability error, and records interrupted status. It does not infer
hardware speed from bit width. The new hand HLS export currently uses fixed
A32/F18 W22/F21 acc64; the old paired A16/W12 overlay is not a compatible hand
deployment. Narrowing the hand hardware still needs matched HLS/driver changes,
C simulation, synthesis, RTL and board checks after selecting actual weights.

For a selected all12/two-second lab model trained on a NATIVE prepared dataset,
the compatibility export produces the files expected by the existing exporter:

```powershell
.\.venv\Scripts\python.exe -m tools.hand_ai_lab export-native `
    --run .comms-local/ai_lab/right_ablation_v1/all12/seed_42 `
    --out models/hand_right_flex3_selected_v1
```

Ablation, short-duration and continuous-lab datasets are deliberately refused by
that compatibility export. They need an explicit deployment contract update;
do not rename their config to bypass the refusal.

Once BOTH native hand models exist, prepare trained-weight HLS references:

```powershell
.\.venv\Scripts\python.exe -m tools.hand_hls_prepare `
    --left-run models/hand_left_flex3_final_v1 `
    --right-run models/hand_right_flex3_final_v1 `
    --left-data data/hand_processed/left_flex3_final_v1 `
    --right-data data/hand_processed/right_flex3_final_v1 `
    --out .comms-local/hand_hls/trained12_final_v1
```

Use the actually selected native paths, not an earlier candidate. For this
generated hand.cfg, run C simulation first:

```powershell
$vitisRun = "C:\AMDDesignTools\2026.1\Vitis\bin\vitis-run.bat"
Push-Location .comms-local/hand_hls/trained12_final_v1
try {
    & $vitisRun --mode hls --csim --config hand.cfg --work_dir hls_component *> csim_console.log
    if ($LASTEXITCODE -ne 0) { throw "C simulation failed; inspect csim_console.log." }
}
finally { Pop-Location }
```

Do not repeat expensive synthesis just because weights changed if the hardware
architecture/arithmetic is unchanged and parameters are runtime-loaded. Verify
the selected bundle and reuse a matching validated overlay. A changed precision,
layer shape, input contract or cache interface needs matching implementation
and verification. No new bit/HWH is produced by this package.

Read existing synthesis estimates without confusing them with board latency:

```powershell
.\.venv\Scripts\python.exe -m tools.hand_ai_lab hls-summary `
    --input .comms-local/hand_hls/synthetic12_v1 `
    --out .comms-local/ai_lab/hand_hls_estimates_v1.json
```

Integer-reference/cache benchmark of an existing synthetic bundle on Windows:

```powershell
.\.venv\Scripts\python.exe -m tools.hand_ai_lab benchmark-bundle `
    --bundle .comms-local/hand_hls/synthetic12_v1 --allow-synthetic `
    --backend numpy --repeats 10 `
    --out .comms-local/ai_lab/numpy_bundle_benchmark_v1.json
```

This is NumPy timing, **not FPGA speed**. Physical FPGA benchmarking is Ultra96
Linux only. On a separately coordinated, idle board, the same command uses
`--backend fpga --bit NEW_MATCHING_BIT --allow-program-fpga`. It programs an
overlay and must not run beside the current shared worker. It checks exact
integer words, alternates LEFT/RIGHT cache slots and reports cold reload/warm
stage timings. The .bit and same-name .hwh must match the 12-feature hand IP.
Get concrete deployment paths and an idle-board slot with comms before running;
no command in this guide stops someone else's worker.

## 9. What to send back and what to commit

First send the AI test summary, demo console output and whether timeline.html
opens correctly. Then choose the next real recording/capture dataset. No gloves
are needed for that first checkpoint. Do not treat synthetic checks as progress
in final accuracy.

The relevant new files are `src/hand_ai_lab/`, `tools/hand_ai_lab.py`,
`tests/test_hand_ai_lab.py`, `docs/AI_LAB_*.md` and
`docs/ai_lab_collection.example.json`. Keep the seven event-transport files
outside this AI commit. Do not stage `.comms-local`, raw recordings or model
runs blindly. Suggested commit after your checks pass:

`feat(ai): add offline gesture evaluation and controlled training experiments`

Suggested PR title: **Add independent-hand AI evaluation and experiment tools**.
Description: Adds deterministic continuous replay, reviewed annotations, action
metrics and validation-only confirmation selection. Adds grouped window-duration
preparation, multi-seed feature ablations, fixed-point screens and scoped
benchmarks. Existing recording, comms and deployment paths remain unchanged.
Validation: include your actual local test/demo result and any skipped tests.

Preserve selected weights plus matching configuration/normalization, evaluation
reports, raw backups and provenance. File cleanup remains a later reviewed task.
