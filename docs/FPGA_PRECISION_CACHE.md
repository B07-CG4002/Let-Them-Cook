# Two-slot parameter caching experiment

Base checkpoint on `AI-fpga-precision-sweep`: `bee41c5f`, with preparation
commits `5239f61d` and `471bd5a7`. The working tree was clean at that checkpoint.
This package adds a generator, its tests and this guide. Parent files and the
verified production overlay remain separate.

## Why two slots

The two-lane parent still reloads 53,824 weights and 168 biases each inference.
Weight loading alone is 53,827 of 198,946 HLS-estimated cycles, about 27%.
Caching can avoid repeated loading after a slot is initialized. Two slots
allow alternating between parameter sets without continuously evicting one.
This increases parameter storage and may add selection logic; synthesis must
establish the actual tradeoff. No cache speedup or FPGA power saving is claimed.

One CNN engine serves both slots sequentially. Input windows, normalization,
confirmation and gameplay states remain the responsibility of the caller.
This benchmark still uses the old 50x22 paired input/eight-class architecture,
A16/F6 activations, W12/F13 weights, signed38 accumulation and 32-bit AXI words.
It does not deploy the intended independent 12-feature/seven-class models.
The probe models in its tests are analytical test cases, not trained hand models.

## Explicit cache contract

The new top is `gesture_cnn_precision_u2_cache`, returning an unsigned32 status.
Its arguments are input words, parameter words, output words, cache slot,
model ID and cache command. Scalars use AXI4-Lite; refer to the GENERATED
register map before writing any future board driver. No old offsets are assumed.

| Command | Value | Behavior |
|---|---:|---|
| AUTO | 0 | Infer; load on invalid slot or mismatching model ID |
| FORCE_RELOAD | 1 | Reload the selected slot's weights AND biases, then infer |
| INVALIDATE_ALL | 2 | Clear both valid flags, write zero output, skip inference |

| Returned status | Value |
|---|---:|
| HIT | 0 |
| LOADED | 1 |
| INVALID_CONTROL | 2 |
| INVALIDATED | 3 |

Slots 0 and 1 are valid; other slots or commands >2 return INVALID_CONTROL,
write zero output and leave cache contents unchanged. A hit intentionally
ignores parameter bytes. The host must use a different model ID or FORCE_RELOAD
when changing a model; IDs must be assigned without collisions among active
parameter sets. Buffer pointers must remain valid even on a hit. Bounds and
scales must be checked by the host before providing inference data/parameters.

Valid flags have an explicit HLS RESET pragma. Parameters themselves are not
cleared on reset; an invalid flag prevents their reuse until loading completes.
Cold initialization, explicit invalidation and reloads are tested. A physical
mid-run reset and subsequent automatic reload still require board validation.

## Install and prepare on Windows

No gloves or Ultra96 connection are required. Run at repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_precision_hls_cache.py" -v

.\.venv\Scripts\python.exe -m tools.precision_hls_cache `
    --parent .comms-local/precision_hls/a16_w12_u2_v1 `
    --out .comms-local/precision_hls/a16_w12_u2_cache2_v1
```

There are 18 new tests. Six portable C++ tests are optional and skip if `g++`
is not on PATH; no extra compiler installation is required for this workflow.
Vitis C simulation below exercises the actual HLS types and all cache cases.

Preparation verifies parent source identity/hashes, precision, vectors and
parameter ranges. It reuses the original vectors and normalization contract.
It refuses existing output directories and generates `cache_contract.json`,
sources, a cache-aware testbench and three configurations. No running process
or FPGA is touched.

## First checkpoint: Vitis C simulation and synthesis

```powershell
Push-Location ".comms-local\precision_hls\a16_w12_u2_cache2_v1"
try {
    & "C:\AMDDesignTools\2026.1\Vitis\bin\vitis-run.bat" `
        --mode hls --csim --config precision.cfg --work_dir hls_component 2>&1 |
        Tee-Object -FilePath "csim_console.log"
    if ($LASTEXITCODE -ne 0) { throw "Cached C simulation failed." }

    & "C:\AMDDesignTools\2026.1\Vitis\bin\v++.bat" `
        -c --mode hls --config precision.cfg --work_dir hls_component 2>&1 |
        Tee-Object -FilePath "synthesis_console.log"
    if ($LASTEXITCODE -ne 0) { throw "Cached synthesis failed." }
} finally {
    Pop-Location
}
```

Expected C-simulation results: zero raw-word mismatches; 24/24 class agreement;
zero regression cache-status errors; 17/17 cache-control cases. The unchanged
6/24 correctness is on the old recordings, not a final accuracy assessment.

The 17 extra cases check cold/warm behavior, alternating slots, same-ID byte
changes intentionally retaining cached values, forced weight-only and bias-only
updates, model-ID changes, invalidation, invalid controls and slot preservation.
The generator's optional native tests deliberately break reload/identity logic
and verify that the testbench rejects those implementations.

Upload `gesture_cnn_precision_u2_cache_csynth.rpt` and the C-simulation summary.
Compare BRAM/DSP/LUT/FF, loop II and timing against the parent (55 BRAM_18K,
10 DSP, 4,715 FF, 6,341 LUT, 198,946 estimated cycles). Extra resident parameters
are a deliberate resource cost, not an unexpected precision regression.

## Later RTL validation and timing modes

After synthesis review, the validation command is:

```powershell
Push-Location ".comms-local\precision_hls\a16_w12_u2_cache2_v1"
try {
    & "C:\AMDDesignTools\2026.1\Vitis\bin\vitis-run.bat" `
        --mode hls --cosim --config precision.cfg --work_dir hls_component 2>&1 |
        Tee-Object -FilePath "cosim_console.log"
    if ($LASTEXITCODE -ne 0) { throw "Cached RTL validation failed." }
} finally {
    Pop-Location
}
```

IMPORTANT: validation includes invalid-control/invalidate calls that do no
inference. Its minimum latency is NOT warm inference latency. Use the separate
performance configurations for speed comparisons:

- `precision_perf_cold.cfg`: 24 real regression inputs, force a full load on
  each call; alternates slots; no invalid/clear calls.
- `precision_perf_warm.cfg`: two preload calls followed by 24 real regression
  inputs alternating initialized slots, each required to return HIT. Both
  slots use the same known reference weights with different IDs for timing;
  differing-model correctness is covered by the 17 validation cases.

Use separate work directories `hls_perf_cold` and `hls_perf_warm`, respectively,
to avoid configuration/argv carryover and preserve all reports. Each directory
must have its own synthesis before RTL co-simulation. Commands follow the same
pattern as above with the corresponding config and work_dir. First check C
simulation for each mode. The warm report includes two cold preload calls:
do not describe its aggregate average as a pure warm average. Review the latency
distribution and per-transaction traces if needed. These performance runs are
later checkpoints, not requirements before the first synthesis review.

## Evidence and limits

The parent two-lane RTL run passed zero mismatches/24-class agreement with
198,807 latency cycles and 198,821 interval cycles. That is about 1.988 ms at
100 MHz, not a measurement of the new cache design or a new physical overlay.

Local verification of this cache package: all 18 new tests passed, including
six optional native C++ checks. Portable C++ ran the actual 24 old regression
inputs in validation, cold and warm modes: zero mismatches and 24/24 agreement
in every mode; validation passed all 17 additional cache cases. These native
checks do not substitute for Vitis ap_int, RTL or physical board validation.

Keep the existing deployed worker/bit/HWH available. This new control/return
ABI requires a new reviewed driver and Vivado integration; the production
worker must not be used to run it. No new overlay is produced by this package.

## Primary references

- [AMD static storage and persistence](https://docs.amd.com/r/en-US/ug1399-vitis-hls/Statics)
- [AMD initialization and reset behavior](https://docs.amd.com/r/en-US/ug1399-vitis-hls/Controlling-Initialization-and-Reset-Behavior)
- [AMD block-level control and function return](https://docs.amd.com/r/en-US/ug1399-vitis-hls/Block-Level-Control-Protocols)
