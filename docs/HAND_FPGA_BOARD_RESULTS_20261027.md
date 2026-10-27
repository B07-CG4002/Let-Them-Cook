# Independent-hand FPGA checkpoint — 27 October 2026

## Verified scope

The new 12-feature/seven-class CNN passed Vivado implementation and a synthetic arithmetic/cache regression on the physical Ultra96. LEFT and RIGHT parameter slots were exercised in alternating calls through one serialized compute engine. This does not establish trained gesture accuracy, simultaneous end-to-end action handling, phone/game integration or a permanent independent-hand deployment.

Contract: 50 time positions x 12 features (ax, ay, az, gx, gy, gz, yaw, pitch, roll, flex1, flex2, flex3), seven labels PICKUP/DROP/CHOP/WASH/THROW/POURING/IDLE. Input time positions are resampled from a two-second window. Arithmetic A32/F18, W22/F21, accumulator64; signed32 AXI carriers; two lanes and two parameter-cache slots. Normalization stays on CPU and is selected separately for each hand. Parameters are loaded at runtime.

## Selected hardware artifacts

Instance: gesture_cnn_hand_u2_cache_0, CPU control address 0x80000000. Part: xazu3eg-sbva484-1-i.

| File | SHA256 |
|---|---|
| cnn_hand12_v1.bit | b8d0407300c913fbde24143b6a632f62c0bf4021290d80fad575d32f59d6523d |
| cnn_hand12_v1.hwh | 935975fe769d0fda1a802c43c9a65430406a61dc02802f637df40f935660a790 |

Keep this pair together with its artifact_manifest.json. Its original built_pending_board_validation status is historical build-time metadata; the board results below and raw reports document subsequent validation. It is not a trained model bundle. Preserve the future selected weights, matching configuration and normalization separately.

## Vivado implementation

Vivado 2026.1 completed the build at C:/cg4/h12 on 27 October 2026. The earlier repository-local build stopped during IP generation because a generated path exceeded the Windows 260-byte limit; it was retried at the short path without changing the HLS design.

| Check | Result |
|---|---|
| PL clock | 100 MHz, 10 ns |
| Setup WNS | +1.282 ns |
| Hold WHS | +0.010 ns |
| Pulse-width WPWS | +3.500 ns |
| Timing failing endpoints | 0 for setup, hold and pulse width |
| Unconstrained internal endpoints | 0 |
| LUTs | 7,366 / 70,560 (10.44%) |
| Registers | 10,167 / 141,120 (7.20%) |
| Block RAM tiles | 78 / 216 (36.11%) |
| DSPs | 20 / 360 (5.56%) |

DRC reported DSP input/product/output pipeline warnings; bitstream generation completed with zero DRC errors. Generated HDL explicitly ties PS HPC1 ARUSER and AWUSER inputs to zero. Both CNN AXI masters map low 2GB DDR, with OCM excluded. The original PS configuration was retained as the seed; Vivado refreshed 12 SD-interface tap-delay settings in saved metadata. DDR-memory and PL-clock settings matched the reviewed source. No boot image or PS initialization files were deployed by this test.

## Physical-board regression

Environment: Python 3.10.4, NumPy 1.21.5, PYNQ 3.0.1. The test used the existing synthetic12_v1 HLS bundle, not recorded gestures or trained LEFT/RIGHT models.

- Eight distinct golden samples: four for each hand, alternating.
- Exact integer words matched on all 56 measured calls: eight golden, 16 force-reload and 32 warm alternating calls.
- Golden class agreement: 8/8. Exact-word mismatches: 0. Cache-status errors: 0.
- Two additional warm preload calls were also checked, but excluded from timing statistics.
- Maximum golden logit error against floating-point reference: 2.280808985233307e-06.
- The physical test exercised first load, forced reload and warm alternation; it did not repeat all 17 RTL cache-control cases.

| Mode | Calls | Mean (ms) | Median (ms) | p95 (ms) | Maximum (ms) |
|---|---:|---:|---:|---:|---:|
| Golden alternating | 8 | 1.334170 | 1.192177 | 1.825661 | 1.949598 |
| Cold forced reload | 16 | 1.543623 | 1.540617 | 1.563030 | 1.568333 |
| Warm alternating | 32 | 1.193819 | 1.188772 | 1.225152 | 1.246560 |

Timing includes run_words input copy/flush, pointer/control writes, AXI start/poll and output invalidate/copy. It excludes normalization, window construction, softmax, model/overlay loading and communication. These timings should not be treated as end-to-end gesture latency or compared directly with an older benchmark using a different measurement boundary.

## Restoration and checker correction

The communications server (PID 165927) remained running. The original paired eight-class worker was restored as PID 177087/start_ticks 611890431 and connected to the bridge. A fresh Python process confirmed the original bitstream SHA256:

4a159424f60be3f10d089f81142c71d3450101e0440f7dcdfbd6c6d53f55cd54

Checker v1.0 incorrectly read process-local PYNQ Device metadata after another process restored the original overlay, producing a restoration verification error. Preserve that raw audit_summary.json unchanged alongside restoration_followup.json; do not describe the original log as error-free. Checker v1.1 verifies restoration through a fresh interpreter and adds regression tests. It changes no hardware or inference arithmetic, and the successful board comparison was not repeated.

Original evidence folder: /var/tmp/cg4002-hand12-board-i555vbro. Backed-up archive: hand12-board-evidence-20261027.tar.gz. The historical checker was pinned to pre-test worker PID 175365 and must not be rerun against the restored worker without a new reviewed identity.

The shared production service still uses the original 22-feature/eight-class model and paired full-CNN overlay. The new overlay was tested temporarily and is not the active deployment.

## Remaining work

1. Final sensor mounting, repeatability, orientation and hand/GPIO mapping checks.
2. Fresh 12-feature recordings and session/person provenance for each hand.
3. Train and evaluate per-hand models, then test continuous transitions and independent action confirmation.
4. Compare trained exports against integer/FPGA outputs; repeat numeric precision screening on the selected data/model.
5. Coordinate v4 transport, independent-hand worker and phone/game integration with teammates.

The seven experimental hand-event transport files have an unresolved integration-test failure and are outside this completed hardware checkpoint. They must not be represented as validated by these results.
