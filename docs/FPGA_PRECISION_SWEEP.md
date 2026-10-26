# CPU precision exploration

This tool profiles GestureCNN ranges and simulates narrower fixed-point formats.
It creates reports only. It does not change model weights, normalization,
firmware, HLS sources, the running worker, or any FPGA overlay.

## Verified starting point

On 26 October 2026, the full-CNN overlay with standardized October 21 eight-class
weights matched all 24 exact reference outputs on Ultra96. There were zero
output-word mismatches, 24/24 floating-point class agreement, maximum logit error
6.818771362304688e-05, mean measured accelerator call 2.056512632407248 ms and
maximum 2.1124444901943207 ms. Timing excludes input packing/flush and model/overlay
loading. The original worker was restarted and bridge connectivity confirmed.

Those older input recordings scored 6/24 in software and hardware. They are a
regression fixture, not final glove accuracy. The active deployment remains
paired 22-feature/eight-class inference. The new three-flex pipeline is separate
12-feature/seven-class inference; its FPGA migration remains outstanding.

## Installation and first run (Windows PowerShell)

Use branch `AI-fpga-precision-sweep`. Extract the package into the repository root.
It adds only these three files; no existing model/firmware files are replaced:

- `tools/precision_sweep.py`
- `tests/test_precision_sweep.py`
- `docs/FPGA_PRECISION_SWEEP.md`

NumPy is required. PyTorch is only needed for the optional neural-network
cross-check test and when reading a trained three-flex run. Existing capstone
environments should already contain both; no new dependency is required.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_precision_sweep.py" -v

.\.venv\Scripts\python.exe -m tools.precision_sweep `
  --weights fpga/hls/eightclass_weights_standard_20261021.npz `
  --data fpga/hls/eightclass_board_test_20261019.npz `
  --activation-int-bits 10 `
  --out reports/precision/paired_standard_20261026_v1
```

Use a NEW output directory each time. Existing output is refused. The legacy
command explicitly uses 10 activation integer bits, reproducing the first
exploration on these older raw inputs. This is not a final safe range guarantee.
The same program runs on the board with NumPy, but a Windows run avoids using
Ultra96 CPU during a teammate's integration session. No gloves are required.

## Meaning of formats

`W` is total signed width; `F` is fractional width; `I = W - F` includes sign.
Integer quantization is `floor(value * 2**F + 0.5)`, ties toward positive infinity.
Values outside the signed width are saturated and counted. Biases use the same
width/fraction as activations, matching the current baseline policy. Layer sums
use exact int64, with one rounding/saturation per output before activation.

The initial candidate sweep uses activation widths 24, 20, 18, 16 and weight
widths 16, 12, 10, 8. It also evaluates the current numerical format A32/F18,
W22/F21 as a control. Internal weight scaling is reselected to fit the actual
weights; the immutable weight tensor determines its range. For these weights,
the largest absolute value is approximately 0.1423, so the common scaled range
is [-0.25, 0.25). A12-bit weight therefore has F13 and I=-1. Such scaled formats
are supported in fixed-point arithmetic; this is not an ordinary Q1.11 format.

Without `--activation-int-bits`, activation I is derived from calibration float
input/intermediate ranges with one extra range guard bit. `--guard-bits` changes
that margin. An explicit I overrides automatic selection and its guard bits.
Weights share a common I by default; `--weight-int-bits` can override it.
This first tool reports per-layer ranges but uses UNIFORM activation and weight
formats within each candidate. It does not yet implement mixed precision.

Example smaller search:

```powershell
.\.venv\Scripts\python.exe -m tools.precision_sweep `
  --weights fpga/hls/eightclass_weights_standard_20261021.npz `
  --data fpga/hls/eightclass_board_test_20261019.npz `
  --activation-int-bits 10 `
  --activation-widths 20,16 --weight-widths 12,10 `
  --out reports/precision/paired_standard_20261026_v2
```

## Reading results

- `range_profile.json`: model/data hashes, class/feature contracts and calibrated
  float ranges. Legacy mode reuses the regression fixture for range profiling;
  this is explicitly labelled and cannot establish a production range.
- `candidates.csv`: widths/fractions, class agreement, clipping, probability
  changes, threshold changes, filtered-label changes and software screen result.
- `sweep.json`: full results, changed sample indices, partial accumulator extrema,
  per-layer accumulator full-range bounds and run status. Interrupted runs retain
  completed rows and status `interrupted`; they are not completed sweeps.

`passes_regression_screen` means no clipping, no top-class changes, no changes
in confidence-filtered labels, and maximum absolute softmax probability error
at most 0.01. This one-percentage-point error limit is a proposed starting rule,
not a course requirement or proof of model quality. Override with
`--max-probability-error` to explore a different documented tolerance.

The default threshold is >=0.70, matching the deployed socket worker. To model
a strictly-above display use `--threshold-rule gt`. A filtered-label change
means class/confidence filtering changed; stable confirmation, cooldowns and
game states are NOT simulated. Identical top classes alone do not establish
identical emitted output.

Measured accumulator widths include every intermediate partial sum, not just
the final dot product. They describe the provided inputs ONLY. The separate
full-range bounds cover all representable activation/weight operands plus this
model's aligned quantized biases and rounding term. Do not size hardware from
measured peaks alone. Candidates whose conservative sums exceed int64 are
refused before any arithmetic that could silently overflow.

CPU simulator execution time is NOT an FPGA latency prediction. A narrow
candidate does not prove DSP/BRAM/LUT savings. AXI buffers in the current driver
also remain 32-bit words until a coordinated packing/interface change is made.
Each HLS candidate must implement exactly the selected scaling, bias alignment,
rounding and saturation; the existing F21 shift constants cannot simply remain.

## Final trained three-flex model (later, when available)

Use a trained `src.hand_ai_flex3.pipeline` run and the exact prepared dataset
that trained it. Do not point this command at the old eleven-feature right-hand
v3 model. Replace the two CAPITALIZED paths below with actual directories:

```powershell
.\.venv\Scripts\python.exe -m tools.precision_sweep `
  --run models/YOUR_TRAINED_THREE_FLEX_RUN `
  --data data/hand_processed/YOUR_THREE_FLEX_DATASET `
  --split validation `
  --out reports/precision/three_flex_validation_v1
```

This mode checks dataset hashes against training provenance, hand identity,
class order, model shapes, session separation and normalization. It applies the
saved mean/std once, profiles training inputs, and evaluates validation inputs.
It also checks the NumPy reference against PyTorch before any sweep. A mismatch
aborts the run. Keep the test set untouched during candidate selection; explicit
`--split test` consumption is recorded in the output. Future rolling-window
recordings, reset/preparation movements and unseen hardware sessions require
separate validation; this script is not that live replay.

## Next hardware experiments

Retain only software candidates worth testing. Then measure actual HLS/Vivado
resource use, achieved II/clock and timing, and run exact FPGA reference tests.
After selecting precision, vary partial loop unroll factors separately (e.g.
1/2/4), and assess parameter caching separately. Maintain a verified baseline
and distinguish the old 22/8 regression model from the final 12/7 architecture.

Suggested commit after checks pass:
`feat: add CPU fixed-point precision profiling and regression sweep`
