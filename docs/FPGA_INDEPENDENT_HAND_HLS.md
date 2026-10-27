# Independent-hand HLS preparation: first migration checkpoint

This checkpoint works without gloves or trained models. It prepares a new
12-feature/seven-class CNN with two calculation lanes and two parameter slots.
It does not change firmware, communications, a running worker or an FPGA overlay.

## What is being checked

- Input: 50 time positions x 12 features, one hand per call.
- Feature order: ax, ay, az, gx, gy, gz, yaw, pitch, roll, flex1, flex2, flex3.
- Finger mapping: flex1 index GPIO34, flex2 middle GPIO35, flex3 thumb GPIO39.
- Output classes: PICKUP, DROP, CHOP, WASH, THROW, POURING, IDLE.
- LEFT: device 1, cache slot 0. RIGHT: device 2, cache slot 1.
- One shared CNN engine; calls execute sequentially. Two slots do not create
  independent compute engines or store gameplay/window state.
- AXI carriers remain signed32. This checkpoint starts at activation32/F18,
  weight22/F21 and accumulator64 to validate the changed shape first.
  It does not inherit A16/F6 W12/F13 as an approved final-model precision.
- Each hand has 52,800 weight words and 167 bias words, 52,967 total.
- Input words are time-major and ALREADY normalized with that hand's training
  mean/std. The FPGA applies no normalization. A future worker must normalize
  before fixed-point encoding and keep separate state for each hand.

## Requirements

Use the repository after merging/pulling the precision/cache PR. These existing
modules are dependencies: tools.precision_sweep, precision_hls_prepare,
precision_hls_unroll2 and precision_hls_cache. The reviewed original HLS sources
under fpga/hls/full_cnn are read and checked; they are not modified.

The ZIP adds only three new files: the generator, its tests and this guide.
Python/NumPy are sufficient for synthetic preparation. PyTorch is needed only
for trained checkpoint export and its optional tests. Portable C++ tests use
g++ when available; on Windows those five tests may skip. Vitis C simulation
then exercises the actual ap_int implementation.

## 1. Prepare without gloves

In Windows PowerShell at the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_hand_hls_prepare.py" -v

.\.venv\Scripts\python.exe -m tools.hand_hls_prepare `
    --synthetic `
    --out .comms-local/hand_hls/synthetic12_v1
```

The generated fixture contains four input windows for each of TWO DIFFERENT
synthetic parameter sets. It includes a thumb-only impulse and randomized
12-channel inputs. Eight alternating reference calls exercise both slots.
The extra 17 control cases check reloads, cache ID changes, invalid controls,
invalidation and preservation of the other slot. These are calculation and
interface checks, never gesture-accuracy measurements.

Existing output folders are refused. For a deliberate new experiment, choose
a new name such as synthetic12_v2; preserve completed evidence.

## 2. Run only Vitis C simulation first

This is the next local checkpoint. No board connection is needed.

```powershell
$vitisRun = "C:\AMDDesignTools\2026.1\Vitis\bin\vitis-run.bat"
if (!(Test-Path -LiteralPath $vitisRun)) { throw "Vitis launcher not found" }

Push-Location ".comms-local\hand_hls\synthetic12_v1"
try {
    & $vitisRun --mode hls --csim --config hand.cfg --work_dir hls_component 2>&1 |
        Tee-Object -FilePath csim_console.log
    if ($LASTEXITCODE -ne 0) { throw "Hand CNN C simulation failed. Send the final log." }
}
finally {
    Pop-Location
}
```

Expected: zero raw-word mismatches, prediction agreement 8/8, zero cache-status
errors, cache-control cases 17/17, followed by CSim done with 0 errors.
The class agreement compares the synthetic fixed-point and float references;
it does not score gestures performed by a person.

The previously diagnosed C:\dev\null issue is not modified by this package.
If the same make error reappears, preserve the log and investigate that file;
do not change global AMD makefiles.

## 3. Optional synthesis and RTL, after C simulation passes

```powershell
$vitisRun = "C:\AMDDesignTools\2026.1\Vitis\bin\vitis-run.bat"
$vitisCompiler = "C:\AMDDesignTools\2026.1\Vitis\bin\v++.bat"

Push-Location ".comms-local\hand_hls\synthetic12_v1"
try {
    & $vitisCompiler -c --mode hls --config hand.cfg --work_dir hls_component 2>&1 |
        Tee-Object -FilePath synthesis_console.log
    if ($LASTEXITCODE -ne 0) { throw "Hand CNN synthesis failed" }

    & $vitisRun --mode hls --cosim --config hand.cfg --work_dir hls_component 2>&1 |
        Tee-Object -FilePath cosim_console.log
    if ($LASTEXITCODE -ne 0) { throw "Hand CNN RTL simulation failed" }
}
finally {
    Pop-Location
}
```

RTL co-simulation may take many minutes. Do not run another command in the same
component while it is active. The new top is gesture_cnn_hand_u2_cache.
Default validation includes short control-only paths, so its reported minimum
must not be described as full inference latency.

Separate hand_perf_warm.cfg and hand_perf_cold.cfg are provided for later
timing characterization. Use different work directories hls_perf_warm and
hls_perf_cold. Warm reports include two initial preload calls in their average.
Recheck actual clock and memory behavior on the board before claiming a speedup.

## 4. Export actual models after recording and training

Keep each real model.pth with config.json, class_names.json, normalization.npz,
training.json and split_manifest.csv, plus its prepared dataset and raw backups.
The two models must use the same feature/class/preprocessing contract, but
must retain their own mean/std and hand identities.

The following paths are EXAMPLES for future models, not models that currently
exist on Wen Ree's laptop:

```powershell
.\.venv\Scripts\python.exe -m tools.hand_hls_prepare `
    --left-run models/hand_left_flex3_v1 `
    --right-run models/hand_right_flex3_v1 `
    --out .comms-local/hand_hls/trained12_v1
```

The generator locates each original prepared dataset via training.json. If
datasets moved, use --left-data PATH and --right-data PATH; hashes still must
match. It verifies checkpoint shapes, hand identity, normalization, dataset
provenance, and agreement of the NumPy/PyTorch float references. It then freshly
checks the high-precision integer outputs against VALIDATION recordings only.
It does not evaluate test accuracy or consume test examples as reference cases.
The dataset-file integrity check does hash the complete prepared dataset files.

Clipping, class changes, confidence-filtered output changes, or probability
error above 0.01 cause export to fail before the output directory is created.
The probability limit is an initial engineering screen, not a course criterion.
No failing candidate is silently accepted. A failure needs investigation and
possibly a different numeric format; it does not automatically mean retraining.

Generated left/ and right/ directories contain the separate parameter words,
normalizers and configurations. manifest.json records model/data/generated
hashes. Preserve the source trained runs too. Synthetic output configs explicitly
say SYNTHETIC_TEST_ONLY and must never be substituted for trained run artifacts.

## Remaining integration work

After this checkpoint: final sensor fitting and validation; real recording and
training; trained-model numeric validation; v4/12-channel communications; a
new independent-hand worker/driver and action confirmation; Vivado bit/HWH
generation; controlled board deployment and end-to-end testing.

The current production paired driver and register offsets are incompatible
with this new top and controls. A later driver must use the matching HWH map,
serialize shared-engine calls, preserve per-hand windows/normalization/state,
check return statuses and suppress stale results. This ZIP does not implement
that worker or change the shared sensor codec.
