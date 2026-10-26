# Two-lane full-CNN precision experiment

This prepares a NEW HLS experiment from `a16_w12_v1`. It preserves that
experiment's A16/F6 activations, W12/F13 weights, 38-bit accumulator, 32-bit
AXI carriers, parameter reload policy, vectors, target part and 10 ns clock.
It changes the four dot products to two independent accumulation lanes and
adds cyclic factor-2 banking to weights/input/pool1/pool2/hidden. Only the
two-element partial-sum arrays are completely partitioned.

All dot lengths (66, 96, 704, 64) are divisible by two. Lane zero starts with
the aligned bias, lane one with zero. Their final sums are added before the
existing rounding/saturation. There is no intermediate requantization.
The full representable-operand bound covers each lane and their reduction;
it does not rely on the observed 24 recordings being representative.

## Verified parent evidence, 26 October 2026

The existing benchmark uses the older paired 22-feature/eight-class model,
not final independent-hand models. The 24 test recordings are a regression
set; their 6/24 label accuracy is not a final accuracy assessment.

| HLS estimate | A32/W22 baseline | A16/W12 parent |
|---|---:|---:|
| BRAM_18K | 90 | 53 |
| DSP | 6 | 6 |
| FF | 7,728 | 6,366 |
| LUT | 6,548 | 5,840 |
| Latency cycles | 339,224 | 339,356 |
| Clock target | 10 ns | 10 ns |
| Estimated period | 7.300 ns | 7.300 ns |

The parent Verilog C/RTL co-simulation passed with zero word mismatches and
24/24 class agreement. Both uploaded reports describe the SAME run, not two
independent validations. Reported latency was 339,362 cycles, interval
339,376 cycles, total execution 8,145,010 cycles. At the configured 100 MHz,
latency is 3.39362 ms. The 15m13s wall-clock simulation duration is separate.
No new candidate has been validated on the physical board.

## Install and prepare, Windows PowerShell at repository root

Continue on `AI-fpga-precision-sweep` with the prior precision tools installed.
This package adds only a generator, tests and this guide; it does not replace
parent experiment files or production FPGA sources.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_precision_hls_unroll2.py" -v

.\.venv\Scripts\python.exe -m tools.precision_hls_unroll2 `
  --parent .comms-local/precision_hls/a16_w12_v1 `
  --out .comms-local/precision_hls/a16_w12_u2_v1
```

The tool checks the parent manifest and all seven generated files, regenerates
the reviewed parent source to reject manual pragma/source changes, validates
vector framing and parameter ranges, and refuses an existing output directory.
It reuses binary files without changing bytes. Logs/build folders are not copied.
The new top name is `gesture_cnn_precision_u2`.

## First checkpoint: C simulation, then synthesis

Run from the repository root. This script stops if either stage fails and saves
the output inside the NEW experiment. A missing Vitis executable is a setup
failure, not evidence about the CNN. The established executable paths below
were verified on Wen Ree's PC.

```powershell
$vitisRun = "C:\AMDDesignTools\2026.1\Vitis\bin\vitis-run.bat"
$vpp = "C:\AMDDesignTools\2026.1\Vitis\bin\v++.bat"

Push-Location ".comms-local\precision_hls\a16_w12_u2_v1"
try {
    & $vitisRun --mode hls --csim --config precision.cfg --work_dir hls_component 2>&1 |
        Tee-Object -FilePath "csim_console.log"
    if ($LASTEXITCODE -ne 0) { throw "Two-lane C simulation failed." }

    & $vpp -c --mode hls --config precision.cfg --work_dir hls_component 2>&1 |
        Tee-Object -FilePath "synthesis_console.log"
    if ($LASTEXITCODE -ne 0) { throw "Two-lane synthesis failed." }
} finally {
    Pop-Location
}
```

Send the C-simulation summary and the new top-level synthesis report. Find the
report without depending on Vitis directory-layout details:

```powershell
Get-Content ".comms-local\precision_hls\a16_w12_u2_v1\csim_console.log" -Tail 35

Get-ChildItem -LiteralPath ".comms-local\precision_hls\a16_w12_u2_v1" `
    -Recurse -File -Filter "gesture_cnn_precision_u2_csynth.rpt" |
    Select-Object -ExpandProperty FullName
```

Compare cycles, achieved dot-loop II, DSP/LUT/FF/BRAM and estimated period
against A16/W12 parent. II=1 is a request, not a guarantee. Extra lanes and
memory banks may increase resources. Smaller operands alone did not improve
the parent's cycle count; do not promise a twofold end-to-end speedup here.
Weights still reload on every call and remain 32-bit words over AXI.

## Later checkpoints after reviewing synthesis

If the new tradeoff is useful, run its OWN C/RTL co-simulation:

```powershell
Push-Location ".comms-local\precision_hls\a16_w12_u2_v1"
try {
    & "C:\AMDDesignTools\2026.1\Vitis\bin\vitis-run.bat" `
        --mode hls --cosim --config precision.cfg --work_dir hls_component 2>&1 |
        Tee-Object -FilePath "cosim_console.log"
    if ($LASTEXITCODE -ne 0) { throw "Two-lane RTL co-simulation failed." }
} finally {
    Pop-Location
}
```

Only after RTL validation should factor 4, model-weight caching or clock changes
be considered as separately recorded experiments. Factor 4 requires handling
the 66-term Conv1 tail and reviewing bank conflicts; this generator deliberately
supports factor 2 only. A physical build additionally needs reviewed Vivado
integration and a driver that uses the candidate's scaling and IP identity.
Do not load this experiment with the current production worker or overwrite
the known-good bit/HWH. Vitis preparation/simulation/synthesis alone generates
no deployed overlay.

## Local verification of this package

- 12 generator/integrity/bounds tests passed in the assistant's environment.
- Portable C++ compiled and ran the same 24 legacy regression vectors with
  zero mismatches across 192 output words, 24/24 class agreement and maximum
  logit error 0.0256686, exactly as the parent numeric contract.
- The portable check uses native int64 with bounds assertions; it is not
  Vitis ap_int simulation, RTL validation or hardware timing evidence.
- Windows validation completed: all 41 precision tests passed, including
  the NumPy/PyTorch architecture comparison.
- Two-lane Vitis C simulation and Verilog C/RTL co-simulation passed:
  zero output-word mismatches, 24/24 class agreement, max logit error 0.0256686.

## Verified two-lane results: 26 October 2026

| Metric | One-lane A16/W12 | Two-lane A16/W12 |
|---|---:|---:|
| HLS latency cycles | 339,356 | 198,946 |
| RTL co-simulation latency cycles | 339,362 | 198,807 |
| BRAM_18K | 53 | 55 |
| DSP | 6 | 10 |
| FF | 6,366 | 4,715 |
| LUT | 5,840 | 6,341 |
| Clock target | 10 ns | 10 ns |
| Estimated clock period | 7.300 ns | 7.300 ns |

RTL latency decreased by approximately 41.4% at the same configured clock.
These results concern the older paired 22-feature/eight-class benchmark.
Its 6/24 label accuracy is separate from numerical agreement.
Physical board validation and final independent-hand accuracy remain pending.

Evidence: `reports/precision/a16_w12_u2_20261026_230026/`.
The manifests describe preparation settings; the reports record subsequent
synthesis and RTL validation.

## References

- [AMD Vitis 2026.1 loop unrolling](https://docs.amd.com/r/en-US/ug1399-vitis-hls/Unrolling-Loops)
- [AMD Vitis 2026.1 memory access and performance](https://docs.amd.com/r/en-US/ug1399-vitis-hls/Array-Accesses-and-Performance)
- [AMD Vitis 2026.1 command-line HLS flow](https://docs.amd.com/r/en-US/ug1702-vitis-accelerated-reference/Creating-HLS-Components-from-the-Command-Line)
