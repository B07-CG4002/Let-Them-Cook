# Independent-hand Vivado build

This prepares a new local FPGA project from the reviewed full-CNN block design dated 20 October 2026. It retains the complete processing-system configuration (930 saved parameters), LPDDR4 configuration, ~100 MHz PL clock, reset wiring, CPU control path, and two DDR master paths. It removes the old paired accelerator from a copied seed design, then creates and connects the independent-hand accelerator with Vivado Tcl.

The original project, source BD, HLS experiment, FPGA board and running service are not modified. The output directory must be new. Each experiment snapshots the packaged IP RTL, metadata, builder and scripts with hashes. No old custom-IP repository is needed by the new project.

## Hardware contract

| Item | Value |
|---|---|
| Part | xazu3eg-sbva484-1-i |
| Instance | gesture_cnn_hand_u2_cache_0 |
| Input per call | 50 positions x 12 normalized features |
| Output | 7 logits: PICKUP, DROP, CHOP, WASH, THROW, POURING, IDLE |
| Compute | One CNN engine, two parameter-cache slots; calls serialized |
| Arithmetic | A32/F18, W22/F21, accumulator64; signed32 AXI carriers |
| LEFT / RIGHT | Device1 / slot0; device2 / slot1 |
| Control | CPU M_AXI_HPM0_LPD -> axi_smc -> s_axi_control, 0x80000000 / 64K |
| Memory | gmem0 and gmem1 -> axi_smc_1 -> S_AXI_HPC1_FPD; low 2GB DDR mapped; OCM excluded |
| Clock/reset | Existing PS pl_clk0 and proc_sys_reset peripheral_aresetn |

The FPGA does not normalize inputs or manage rolling windows. The CPU selects each hand's normalization and parameters. The previous paired worker/driver and old 11-feature model are incompatible with this IP. A16/W12 results from the legacy paired experiment have not established accuracy for this new hand model; this build retains the arithmetic already tested for the 12-feature design.

## 1. Install and check preparation code (Windows PowerShell)

Save hand-vivado-builder.zip to Downloads. Run from your repository:

```powershell
Set-Location "C:\Users\ooiwe\OneDrive\Documents\CG4002\Let_Them_Cook"
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\hand-vivado-builder.zip" -DestinationPath . -Force
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_hand_vivado_prepare.py" -v
if ($LASTEXITCODE -ne 0) { throw "Builder tests failed. Send the output." }
```

Only new builder/template/test/guide paths are included. No existing model, training, runtime or comms files are replaced.

## 2. Snapshot the reviewed hardware inputs

Use a short build directory. The original repository-local build failed during IP generation at the Windows 260-byte path limit. The successful build used C:/cg4/h12. The preparer now checks projected packaged-IP and generated-IP paths before creating output. This is a preventive check for known file paths, not a guarantee about every temporary path created by Vivado. Existing completed builds need no regeneration.

```powershell
.\.venv\Scripts\python.exe -m tools.hand_vivado_prepare prepare `
    --source-bd "$env:USERPROFILE\Documents\VivadoSources\B07_full_cnn\multiply_accumulate\multiply_accumulate.bd" `
    --hls .comms-local/hand_hls/synthetic12_v1 `
    --out C:/cg4/h12
if ($LASTEXITCODE -ne 0) { throw "Preparation stopped. Send the output." }
```

A source design that differs from the reviewed upload, incomplete packaged IP, changed HLS sources, incompatible format or existing output directory is refused. Text line-ending differences in the source BD do not change its reviewed identity. The HLS manifest's old pending-status fields are retained as historical generator metadata; the tool does not convert them into a claim of completed hardware testing.

## 3. Validate in Vivado first

```powershell
$pythonExe = (Resolve-Path -LiteralPath ".\.venv\Scripts\python.exe").Path
powershell.exe -NoProfile -ExecutionPolicy Bypass `
    -File "C:\cg4\h12\runner\run.ps1" `
    -Mode Validate -Python $pythonExe
if ($LASTEXITCODE -ne 0) { throw "Vivado validation stopped. Send the final output." }
```

The process-scoped execution policy permits this downloaded runner for this invocation only. It does not change your system policy.

Wait for `RUN FINISHED: Validate completed. No FPGA was programmed.` Send that final output before the first long build. If validation fails, retain the experiment and its logs. A corrected retry should use C:/cg4/h12_v2, updating paths in every following command. Do not delete the old project to make a script continue.

## 4. Build after validation succeeds

```powershell
$pythonExe = (Resolve-Path -LiteralPath ".\.venv\Scripts\python.exe").Path
powershell.exe -NoProfile -ExecutionPolicy Bypass `
    -File "C:\cg4\h12\runner\run.ps1" `
    -Mode Build -Python $pythonExe -Jobs 4
if ($LASTEXITCODE -ne 0) { throw "Vivado build/export stopped. Send the final output." }
```

This generates IP outputs and wrapper HDL, runs synthesis and place/route, saves timing/utilization/clocks/DRC/check_timing reports, then writes the bitstream. It checks setup and hold slack before writing the bitstream. Vivado's normal bitstream DRC remains enabled. Artifact export also requires a complete passing setup/hold/pulse-width timing table and a new-IP HWH. No timing or DRC checks are downgraded.

The PC must remain awake and the terminal open. Runtime depends on the PC; the runner saves full output even when the terminal is quiet. From a second PowerShell terminal, inspect the latest console log:

```powershell
$log = Get-ChildItem -LiteralPath "C:\cg4\h12" -Filter "build_*_console.log" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content -LiteralPath $log.FullName -Tail 40 -Wait
```

Ctrl+C in this second log-view terminal stops the log viewer only. Completion of the build terminal is reported with `RUN FINISHED: Build completed. No FPGA was programmed.`

## Output and next checkpoint

Success exports `C:/cg4/h12/artifacts/`:

- cnn_hand12_v1.bit
- cnn_hand12_v1.hwh (same basename, from this experiment)
- artifact_manifest.json (hashes, format, instance, timing and explicit pending-board status)

Send artifact_manifest.json, results/timing_summary.rpt, results/utilization.rpt and results/check_timing.rpt for review. If export stops because timing or HWH parsing is unfamiliar, send its output and reports; do not rename an old HWH into place.

A generated bitstream alone does not prove physical-board correctness or gesture accuracy. The completed synthetic board check is documented in HAND_FPGA_BOARD_RESULTS_20261027.md; it does not establish gesture accuracy. Board comparison with the new runtime and golden integer vectors is the next separate checkpoint. Coordinate board use before loading it; this package includes no board-programming command and does not change the shared service.

Because parameters are loaded at runtime, this hardware can be built before real recordings. Later trained LEFT/RIGHT weights and normalization can be exported for the same 50x12/seven-class architecture and arithmetic without rebuilding hardware. Changing tensor dimensions, parameter layout, arithmetic, IP interfaces or model architecture requires reassessing/rebuilding it. Fresh hardware fitting and sensor checks remain prerequisites for final training data.

## Validation scope of this delivery

Local tests cover frozen inputs, preservation of PS/DDR settings, refusal of changed/missing inputs, setup/build state checks, timing parsing and HWH selection. Tcl templates were checked for syntactic completeness with Tcl. Tests use fixture RTL and HWH; they are not FPGA simulation or hardware execution. No Vivado/PowerShell executable is available in the preparation environment. Windows Vivado 2026.1 validation, implementation and physical-board synthetic comparison subsequently passed on 27 October 2026; see HAND_FPGA_BOARD_RESULTS_20261027.md. The new path-length guard is separately covered by local regression tests; it changes preparation only, not the tested HDL or artifacts.

## Primary documentation consulted

- AMD Vivado 2026.1 read_bd: https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/read_bd
- AMD Vivado 2026.1 assign_bd_address: https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/assign_bd_address
- AMD Vivado 2026.1 exclude_bd_addr_seg: https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/exclude_bd_addr_seg
- AMD Vivado 2026.1 get_timing_paths: https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/get_timing_paths
- AMD Vivado 2026.1 check_timing: https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/check_timing
- AMD Vivado 2026.1 write_bitstream: https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/write_bitstream
