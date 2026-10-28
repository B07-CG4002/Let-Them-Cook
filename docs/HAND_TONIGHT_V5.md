# Tonight's target

A provisional LEFT and RIGHT model that can be checked with the real gloves
locally and exported for FPGA integration. This is a same-person pilot, not
evidence of accuracy on unseen wearers or a completed glove-to-phone system.

Budget roughly 2–4 hours after returning if the range/collection checks pass.
Typical allowances: 15–30 minutes setup and motion checks, 45–75 minutes
collection, 20–60 minutes training/review depending on the PC, then 30–60 minutes
export/live/integration work. These are planning estimates, not measured run
times for your PC. Hardware faults or a new transport issue can extend them.

## 1. Finish the short range check first

Follow `HAND_WIDE_GYRO_DIAGNOSTIC.md`: Setup, FlashRight, FlashLeft, Record,
Audit, Export. Use natural WASH movement. Do not slow gestures merely to get
under a threshold. The new range is ±1000 deg/s; packets carry gyro x10 in
version 5 and LCS5 stats. Check the other agreed gestures briefly before the
full collection if their motion is stronger than WASH.

During wide capture, a gyro magnitude >=900 deg/s or a raw packet channel at
an int16 endpoint stops the current trial before saving. Completed earlier
trials are retained. Do not change thresholds or scales to bypass that check.

## 2. Collect the larger pilot

Use the seven agreed gesture definitions: PICKUP, DROP, CHOP, WASH, THROW,
POURING, IDLE. FIREEXT is excluded. Both hands can perform the same named
gesture during a shared GO cue, but label each hand according to what that
hand actually does. An inactive hand must not get the active hand's label.

Recommended counts **per gesture, per hand**:

| Split | Trials | Session |
|---|---:|---|
| Train | 30 | P01_tonight_train_20261028 |
| Validation | 4 | P01_tonight_validation_20261028 |
| Test | 6 | P01_tonight_test_20261028 |

This is exactly the native 75/10/15 rule for 40 trials per class. It totals
560 hand trials, or 280 simultaneous pairs. At 2 s preparation plus 2 s capture,
the automatic cycles alone take about 19 minutes; allow time for repositioning,
changing gestures, breaks and review. More copies of one rigid performance do
not guarantee better recognition. Vary natural speed, safe orientation and
starting pose within your team's definition, with consistent glove fit.

Use separate capture sessions for each split. Take a break/remove and refit the
gloves between sessions to test realistic session variation. If rebooting,
keep them still through calibration. Do not randomly split overlapping windows
or label repeated copies as new independent trials. One person across sessions
does not establish generalization to other people.

From the repo root, retain this helper variable:

```powershell
$kit = "$env:USERPROFILE\Downloads\wide-gyro-diagnostic\run_widegyro.ps1"
```

Training session:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step CollectTrain
```

For each of the seven gestures, select the correct LEFT and RIGHT action and
press Record selected once. The GUI preset is **30 additional captures**.
Completed trials are retained and numbering resumes, so do not click another
full 30 merely to resume a partial batch; enter the remaining additional count.
Close the GUI when the seven gestures are complete.

Repeat with the independent validation and test presets:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step CollectValidation
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step CollectTest
```

Each GUI opens after the previous one closes. Validation defaults to 4 and
test to 6; again record each gesture once with that count. The output root is
`data/hand_raw_flex3/tonight_v5`, separate from all earlier diagnostics.

Check progress at any point:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step Progress
```

This checks counts and file quality; `ratio_complete` means the actual counts
obey the split rule, not necessarily that they match the 30/4/6 recommendation.
Verify the displayed counts against the table. Rejected or orphan files block
the check; review them without deleting backups. Do not simply relabel splits.

## 3. Train both hands with one command

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step Train
```

This explicitly validates v5 acquisition metadata, CSV/sidecar/session hashes,
source timing/sequence, finite values, gyro headroom, duplicate input and split
separation before preparing either hand. Both hands must be present. Prepared
arrays and their hashes are validated again before training.

It trains three seeds (42,73,101), 30 epochs each, with the same twelve-input,
seven-class CNN. Each hand gets its own training-only normalizer. All channels
remain in raw recordings. RIGHT normalized flex1 is masked during training;
its first-convolution weights are zeroed in saved checkpoints, so generic FPGA
inference also ignores the faulty RIGHT index. That excluded channel alone
uses identity normalization (mean 0/std 1), preventing small training variance
from amplifying later faulty readings past the FPGA input range. Every active
feature uses training-only statistics. LEFT uses all twelve channels.

Within each seed, choose the best epoch by validation macro-F1 then loss;
select the seed by the same rule. The held-out test is not used for selection.
Selected checkpoints are copied to:

```
.comms-local/hand_tonight/v1/models/selected/left
.comms-local/hand_tonight/v1/models/selected/right
```

The validation reports include per-class F1 and confusion matrices. Review
weak or confused classes before claiming a useful model. Never promise a
particular accuracy from trial count alone. If time is tight, `-Seeds 42`
trains one provisional run per hand; the default three seeds give a stronger
comparison. Choose a NEW `-WorkRoot .comms-local/hand_tonight/v2` for retries;
existing prepared datasets/runs/reports are not overwritten.
Use the same `-WorkRoot` for subsequent Live, ExportFPGA and EvaluateTest steps.

The v5 model configuration is `independent_hand_flex3_gyro_v5_v1`. The old
v4 pipeline/Predictor is deliberately unchanged and will refuse it. Use the
included version-aware `tools.hand_tonight` path. The precision source loader
and ModelBundle now explicitly recognize this v5 model contract as well.

## 4. Two-minute real-glove local live test

Close the recorder/other BLE apps and power both gloves with proper startup
calibration. Then:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step Live
```

Use each gesture with LEFT alone, RIGHT alone, then different actions at the
same time. Check natural IDLE and ordinary transitions for false actions.
The console shows raw label/confidence about once a second per hand plus
confirmed local events. Confirmation starts at >0.70 confidence and three
predictions, with independent hand windows/rearm/state expiry. This policy is
provisional and should be tuned using validation/diagnostic evidence.

`-Hands RIGHT` or `-Hands LEFT` tests one glove only. The two-minute run writes
raw callback data, predictions, local event records and an annotation template
under a fresh `live_<time>` folder. Raw receive order is preserved separately
and a callback-time merged capture is created for later review.

This is real BLE plus local CPU recognition. It sends no game commands and
does not establish FPGA, server, phone or complete integration behavior.

## 5. Numeric/FPGA export

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step ExportFPGA
```

This uses the existing trained-hand export path to check the selected weights
and validation inputs against float, PyTorch and A32/F18 W22/F21 arithmetic,
then emits a two-slot bundle under `fpga_bundle`. It refuses changed provenance,
unsupported ranges, clipping or numeric disagreement.

No Vitis command or FPGA programming runs. Reuse the existing board-verified
hand12 .bit/.hwh if the exported architecture/numeric/parameter contract still
matches. Changing trainable weights alone does not require a new bitstream.
Real-weight FPGA outputs/cache behavior still need to be checked on the board
before deploying the selected models. Do not replace the shared deployment
while another teammate is using it.

The exporter historically labels all freshly generated bundles as not
synthesized; for unchanged hardware, consult the selected existing overlay's
board evidence and the new actual-weight numerical/board tests together.

## 6. Held-out evaluation, once settings are frozen

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step EvaluateTest
```

This explicitly records test use. Do not use those results to choose another
seed, change thresholds or repeatedly tweak the same pilot without declaring
the test set consumed. Further iteration needs a new independent final test.

## 7. What your comms teammate needs for tomorrow

The source change now requires **explicit sensor v5 support**, beyond the
previous v4 hand12 work:

- Use `common/sensor_v5.py`: same 40-byte/12-channel layout; gyro scale 10,
  other scales unchanged. Select v5 explicitly; preserve v3/v4 behavior.
- Accept LCS5 protected source stats and validate hand/boot/version. Decode
  physical units exactly once; never interpret v5 gyro values with v4 scale100.
- Propagate sensor-version selection through launcher, dual bridge, protocol
  validation, server, model callback, checks/export and deployment tooling.
- Preserve the existing v3 GESTURE_RESULT compatibility framing where required;
  sensor version does not by itself change phone result framing.
- Stage the patched `src/hand_runtime.py`, selected v5 model bundle/configs and
  matching normalizers. The older paired `socket_worker_full` does not run the
  hand12 overlay. Confirm staging dependencies for the v5 model contract.
- Verify LEFT/RIGHT routing, one-shot deduplication, ongoing state/end/expiry,
  reconnects and actual phone/game behavior. Compatibility results alone do
  not establish the complete structured hand-event lease contract.

The bridge/server migration is not implemented by this AI-only package. The
local live test provides a useful model checkpoint while the comms teammate
updates that path. Full game integration timing depends on that work too.

## Preserve and review

Keep selected model weights together with config, class order, matching
normalization, training/selection manifests and evaluation reports. Keep raw
recording backups and dataset provenance. The raw data itself should not be
committed merely to share code. Preserve previous model runs for comparison.

Review only this package's code and chosen small reports on your feature
branch; avoid unrelated HAND_EVENT experiments. Commit/PR after local checks,
then coordinate the main/deployment update. This is not the final cleanup
milestone; hardware, real-model FPGA and integration validation remain.

## Validation limits of the supplied kit

Locally executed checks cover real C++ conversion/readback and C++/Python
wire scaling for v3/v4/v5, version rejection, mocked BLE rate control, source
and file audits, split/tamper checks, one/both-glove recording, and a timed
mocked two-hand live flow with ordered callback capture.

PyTorch is unavailable in the preparation environment. Two Torch-dependent
tests (the earlier fallback export test and the new one-epoch two-hand training,
RIGHT-index invariance, held-out gate and FPGA-source loader test) are included
for execution on your Windows PC. Setup requires PyTorch and both PlatformIO
builds before flashing. No claim of trained-model accuracy, physical v5 BLE
success or actual-weight FPGA success is made by these software checks.
