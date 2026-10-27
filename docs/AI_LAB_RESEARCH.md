# Research basis and experiment decisions

Reviewed 27 October 2026 for this project. Links below are primary research or
official documentation. Proposed experiments are not measured improvements.

## 1. Prevent optimistic evaluation from overlapping windows

Dehghani et al., *A Quantitative Comparison of Overlapping and Non-Overlapping
Sliding Windows for Human Activity Recognition Using Inertial Sensors* (Sensors,
2019), studies how overlap interacts with subject-dependent/independent
evaluation. Its results motivate treating adjacent windows as correlated and
separating groups before extracting them. It does not establish the best
window size for these cooking gestures.

https://pmc.ncbi.nlm.nih.gov/articles/PMC6891351/

Project implementation: explicit person/session split ownership, hashed captures
and duplicate-input checks before training. Duration candidates use the same
capture groups. Do not interpret hundreds of neighboring windows as hundreds
of independent physical trials.

## 2. Fit transforms using training data and evaluate the intended population

The scikit-learn common-pitfalls documentation describes leakage through fitted
preprocessing. Its grouped-cross-validation documentation describes keeping a
group out of the training fold. A session-separated evaluation and an unseen-
person evaluation answer different questions; report which one was measured.

https://scikit-learn.org/stable/common_pitfalls.html

https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.GroupKFold.html

Project implementation: normalizers fitted on train only, explicit per-hand
models, session checks, validation-only parameter sweeps and explicit test-use
flags. The raw audit warns when people overlap between train and test. It does
not automatically reshuffle recordings to produce more flattering results.

## 3. Make training experiments reproducible

PyTorch's reproducibility notes describe controlling Python/NumPy/PyTorch random
sources and deterministic algorithms, while noting that reproducibility is not
guaranteed across every release/platform. Multiple seeds reveal optimization
variability on a given split; they do not replace independent data.

https://docs.pytorch.org/docs/stable/notes/randomness.html

Project implementation: CPU training, seeded loader, deterministic algorithms,
recorded library/model-source hashes, three-seed comparisons and validation
macro-F1 checkpoint selection. The controlled noise option is off by default.
The stored histories make changes reviewable instead of relying on one final
accuracy number.

## 4. Quantization needs numerical evidence and matching arithmetic

Jacob et al., *Quantization and Training of Neural Networks for Efficient
Integer-Arithmetic-Only Inference* (CVPR 2018), provides primary research on
integer-only inference and training. It motivates examining quantization's
accuracy/efficiency trade-off; it is not proof that this custom HLS CNN should
use any particular bit width or that QAT is needed immediately.

https://openaccess.thecvf.com/content_cvpr_2018/html/Jacob_Quantization_and_Training_CVPR_2018_paper.html

Project implementation: start with post-training screens using training ranges
and validation inputs, track saturation and confidence-gate changes as well as
argmax agreement. A software candidate is not a hardware format until HLS
rounding/accumulation/AXI packing and the driver match and are verified. If
post-training narrowing loses required accuracy, evaluate QAT as a later,
separate training/deployment change with compatible export arithmetic.

## 5. Parallel arithmetic must have enough memory bandwidth

AMD UG1399 describes loop unrolling to replicate operations and array access
optimizations to provide parallel memory access. These mechanisms trade area,
bandwidth and throughput. Synthesis estimates are not routed timing closure or
measured board latency.

https://docs.amd.com/r/en-US/ug1399-vitis-hls/Unrolling-Loops

https://docs.amd.com/r/en-US/ug1399-vitis-hls/Array-Accesses-and-Performance

Project order: profile total inference stages first; preserve the known two-lane
baseline; use synthesis/RTL evidence to decide whether wider lanes, partitioning,
packed transfers or further caching address an actual bottleneck. Compare DSP,
BRAM, LUT and FF along with cycle count, initiation interval and achievable
clock. Keep paired eight-class results separate from the new hand architecture.

## 6. Project-specific decisions requiring new evidence

- The 2-second history and 0.2-second decision interval are independent settings.
  A full window is needed initially, but steady-state action delay is not simply
  equal to history length. Measure onset-to-acceptance on annotated streams.
- Three confirmations at 0.2-second spacing add about 0.4 seconds from the first
  qualifying prediction to the third when they remain consecutive. This is not
  the total gesture latency; window placement, timing jitter and stale suppression
  also matter.
- Restricting throw styles can reduce overlap while narrowing the supported
  behavior. Agree the protocol, then validate styles actually expected in-game.
- The thumb sensor's value must be measured with controlled retraining on the
  same data/splits. More channels are not automatically better with a poor fit.
- More epochs, deeper networks, a higher confidence threshold and more FPGA
  lanes are candidates only after failure analysis. The toolkit supplies
  repeatable evidence; it does not promise a particular final accuracy or speed.
- Background annotations allow measuring false activations without pretending
  that all transition motion is a correctly trained IDLE class. A learned
  background/rejection class would change the seven-class contract and needs a
  separate model/HLS/interface decision.

## Validation limits of this delivery

The package's deterministic replay, contracts, metrics, grouped preparation,
audit and mocked benchmark wrappers were tested here. PyTorch is unavailable in
the preparation runtime: real training/reference tests are included but must run
on the user's PyTorch environment. No learned-model improvement is claimed.
The HTML JavaScript syntax was checked; a browser executable was unavailable for
visual QA. Physical BLE, Windows execution, actual board timing, new bit/HWH
generation and phone/game integration remain separate tests.
