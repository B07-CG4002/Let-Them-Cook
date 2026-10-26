# Isolated HLS precision benchmarks

These experiments compare precision while retaining the reviewed full-CNN loop
structure, II requests, parameter reload policy and 32-bit AXI carrier words.
They are benchmarks of the existing paired 22-feature/eight-class architecture,
not deployment of the future independent 12-feature/seven-class models.

The repository baseline config targets `xazu3eg-sbva484-1-i` at 10 ns. Generated
configs retain both settings, so comparisons use the same target. Final device
compatibility and Vivado integration must be reviewed before any new overlay.

## Files and branch

Continue on `AI-fpga-precision-sweep`. The package adds the preparation tool,
its tests and this guide. It updates `tools/precision_sweep.py` only to record
the final range-profile status correctly. Existing numerical results remain
valid; rerunning the earlier sweep is unnecessary.

Existing model files, original HLS files, production workers, bitstreams and
HWH files are read only. No Vitis commands or board operations are launched by
the preparation tool. Generated experiments belong in separate NEW directories.
The `.comms-local` directory already holds local deployment artifacts; generated
builds should not be staged with source changes.

## Preparation in VS Code / Windows PowerShell

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_precision_hls_prepare.py" -v
```

Use the completed sweep already produced at
`reports/precision/paired_standard_20261026_v1/sweep.json`.

Prepare a baseline numeric control:

```powershell
.\.venv\Scripts\python.exe -m tools.precision_hls_prepare `
  --weights fpga/hls/eightclass_weights_standard_20261021.npz `
  --data fpga/hls/eightclass_board_test_20261019.npz `
  --sweep reports/precision/paired_standard_20261026_v1/sweep.json `
  --activation-bits 32 --weight-bits 22 `
  --out .comms-local/precision_hls/baseline_v1
```

Prepare the primary narrow candidate:

```powershell
.\.venv\Scripts\python.exe -m tools.precision_hls_prepare `
  --weights fpga/hls/eightclass_weights_standard_20261021.npz `
  --data fpga/hls/eightclass_board_test_20261019.npz `
  --sweep reports/precision/paired_standard_20261026_v1/sweep.json `
  --activation-bits 16 --weight-bits 12 `
  --out .comms-local/precision_hls/a16_w12_v1
```

Optional second candidate: use `--activation-bits 20 --weight-bits 12` and
`--out .comms-local/precision_hls/a20_w12_v1` in the same command.

| Experiment | Activation W/F | Weight W/F | Accumulator W | AXI carrier |
|---|---|---|---|---|
| baseline | 32/18 | 22/21 | 64 | 32 bits |
| a16_w12 | 16/6 | 12/13 | 38 | 32 bits |
| a20_w12 | 20/10 | 12/13 | 42 | 32 bits |

The accumulator uses one extra bit above the recomputed full-range bound,
capped at 64 bits. Bounds include aligned quantized biases and rounding.
Measured data peaks alone are not used to select the accumulator width.

The tool validates completed-sweep status, model/input hashes and reviewed HLS
source identity, handling CRLF checkouts. It recomputes the selected candidate
and refuses clipping, class/filter changes, excessive probability error or an
existing output directory. It creates:

- `precision_cnn.h` / `.cpp`: carrier and internal types explicitly separated.
- `tb_precision_cnn.cpp`: exact word checks, float-class checks, range checks.
- `precision.cfg`: same target/clock/loop directives, separate top name.
- `vectors/precision_test.bin`: format-bound inputs, parameters and expected outputs.
- `reference.npz`, `parameter_words.bin`, `manifest.json`: reproducibility evidence.

## Vitis C simulation and synthesis

Use a Windows terminal with the Vitis 2026.1 environment configured. Check tool
availability first; no installation directory is assumed:

```powershell
Get-Command vitis-run.bat, "v++", v++.bat -ErrorAction SilentlyContinue |
  Select-Object Name, Source
```

If nothing appears, locate/configure the installed toolchain before continuing.
Do not change the experiment sources to address a missing executable.

Inside ONE experiment directory, C simulation is:

```powershell
vitis-run.bat --mode hls --csim --config precision.cfg --work_dir hls_component
```

Expected end result: zero raw-word mismatches and 24/24 prediction agreement.
The printed 6/24 correctness on old recordings is not final gesture accuracy.

After C simulation passes, synthesize with the resolved `v++` executable:

```powershell
& "v++.bat" -c --mode hls --config precision.cfg --work_dir hls_component
```

Use the full path returned by Get-Command if your installation resolves a
different executable name. `vitis-run --syn` is NOT the synthesis command.
Repeat in each experiment's own directory; never share a work directory.
For example:

```powershell
Push-Location .comms-local/precision_hls/baseline_v1
try {
  vitis-run.bat --mode hls --csim --config precision.cfg --work_dir hls_component
  if ($LASTEXITCODE -ne 0) { throw "Baseline C simulation failed." }
  & "v++.bat" -c --mode hls --config precision.cfg --work_dir hls_component
  if ($LASTEXITCODE -ne 0) { throw "Baseline synthesis failed." }
} finally {
  Pop-Location
}
```

Run the same block for `a16_w12_v1` after the baseline succeeds.

Collect reports with:

```powershell
Get-ChildItem .comms-local/precision_hls -Recurse -File -Filter "*csynth.rpt" |
  Select-Object FullName
```

Compare latency in cycles, achieved II, estimated period, DSP, LUT, FF and BRAM.
HLS estimates do not establish routed timing or measured FPGA latency. Select
candidates for C/RTL co-simulation, Vivado implementation and controlled board
tests after reviewing these reports. Change unroll factors and weight caching
in later experiments so their effects are distinguishable from precision.

## Portable host check (optional developer verification)

If g++ is available, from the experiment directory:

```text
g++ -std=c++14 -O2 -DCNN_PORTABLE_TEST precision_cnn.cpp tb_precision_cnn.cpp -o precision_sim
```

Run `precision_sim.exe` on Windows or `./precision_sim` on Linux. This mode uses
native integer storage with accumulator assertions. It tests arithmetic and
serialization, but does not substitute for Vitis ap_int simulation or synthesis.
The testbench searches both its current directory and `vectors/` for the data.

## Deployment boundary

The generated top is `gesture_cnn_precision`, distinct from the production IP.
The production driver assumes F18 activations and F21 weights; it MUST NOT be
used to run these candidates. No new `.bit`/`.hwh` is generated by preparation,
C simulation or C synthesis alone. A later candidate-specific driver and
reviewed Vivado integration are required. Keep the verified production overlay
and weights available throughout.

Preserve selected manifests, reports and vectors with the chosen candidate.
Suggested source commit after validation:
`feat: prepare isolated HLS precision benchmarks and exact reference vectors`

## Command references

- [AMD Vitis 2026.1 command-line HLS flow](https://docs.amd.com/r/en-US/ug1702-vitis-accelerated-reference/Creating-HLS-Components-from-the-Command-Line)
- [AMD Vitis 2026.1 C synthesis](https://docs.amd.com/r/en-US/ug1399-vitis-hls/Running-C-Synthesis)
- [Reviewed project baseline configuration](https://github.com/B07-CG4002/Let-Them-Cook/blob/4b8c2bf7727bdc0beeb16a777f51fe8b971c49b9/fpga/hls/full_cnn/full_cnn_20261020.cfg)
