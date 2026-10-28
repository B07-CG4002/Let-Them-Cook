# Provisional trained hand12 v5 model handoff — 29 October 2026

The LEFT and RIGHT models are trained and have been exercised with the real
gloves through local BLE/CPU inference. Continuous recognition remains
provisional, particularly THROW/DROP transitions and unexpected actions.
This is an integration checkpoint, not final accuracy or deployment approval.

## What to load

Pass the complete directory `fpga/models/hand12_v5_20261029` as the hand12
worker's `--bundle`. Preserve every file and its relative path: the manifest
verifies the generated files, including the generated HLS source/config.

- `left/parameter_words.bin`: LEFT weights and biases, slot 0 / device 1.
- `right/parameter_words.bin`: RIGHT weights and biases, slot 1 / device 2.
- Per-hand `normalization.npz` and `config.json`: matching preprocessing.
- `manifest.json`, `hand_contract.json`, vectors and generated files: version,
  hashes, class order and numeric regression data.

The exported weights use the hand12 runtime's binary format. No additional
old-style paired eight-class weight NPZ is required. `normalization.npz` is
not a weights file. Do not rename a parameter BIN to NPZ or use socket_worker_full.

Selected PyTorch checkpoints and training provenance are also preserved under
`models/hand_v5_20261029/{left,right}` with `selection.json`. These are for
retraining/comparison; FPGA inference consumes the exported binary parameters.
Evaluation reports are under `reports/hand12_v5_20261029` when available.
Raw recordings remain in local backups and are not included in this commit.

## Existing hardware

Reuse the matching checked-in hand12 overlay under
`fpga/overlays/independent_hand_v1/`, with the unchanged 12x50 input,
seven-class CNN, A32/F18 activations, W22/F21 weights, 64-bit accumulator,
two lanes and two parameter-cache slots. A weight-only replacement does not
require re-synthesis. Test the new real weights on the physical FPGA before
claiming their board outputs are validated. Restart/load the matching runtime
so first use forces each hand's parameters into its cache slot.

BIT SHA256: b8d0407300c913fbde24143b6a632f62c0bf4021290d80fad575d32f59d6523d
HWH SHA256: 935975fe769d0fda1a802c43c9a65430406a61dc02802f637df40f935660a790

## Required communications change

Both gloves currently run explicit **sensor v5 / LCS5** firmware. The packet
is still 40 bytes with 12 int16 channels, but gyro scale is **10**, not v4's
100. Gyro values after decoding are degrees/second. Other scales are unchanged.
Use `common/sensor_v5.py`, and decode physical units exactly once.

The reviewed main branch still accepts sensor 3/4 in the launch/bridge path.
Propagate explicit v5 support through source-statistics validation, launcher,
laptop/dual bridge, server/protocol, model callback, staging and deployment.
The current staging helper's generated hand12 deploy command still selects
sensor version 4, so update that path before using it with these v5 gloves.
Do not bypass version checks or reinterpret v5 bytes with the v4 decoder.

Preserve Jie Yan's current `src/socket_worker_hand.py`, hand-event server gate,
phone adapters and compatibility result framing from PRs #37–39. This AI update
changes `src/hand_runtime.py` to accept and validate the explicit v5 bundle
configuration; it does not replace his worker. Sensor version does not itself
change the v3 compatibility GESTURE_RESULT framing.

Do not add the older untracked hand_gateway/socket_worker_hand_events/
hand_event_server experiments. Main already has the newer integration path.

## Model evidence and limits

- 30 train / 4 validation / 6 test recordings per gesture, per hand: 280 per hand.
- Seven labels: PICKUP, DROP, CHOP, WASH, THROW, POURING, IDLE. No FIREEXT.
- Selected seeds: LEFT 101, RIGHT 73. Validation: 28/28 and macro-F1 1.0 each.
- RIGHT index flex1 GPIO34 is temporarily excluded through folded zero weights;
  original raw readings remain preserved. LEFT uses all twelve inputs.
- Float/fixed export: class agreement 56/56, zero threshold-decision changes
  and zero parameter/intermediate clipping events in the screened examples.
- The existing physical overlay evidence used synthetic weights. The new real
  weights still need a board comparison and complete glove-to-game testing.
- Initial table-rest recognition and THROW/DROP transitions produced concerns;
  high confidence alone is not proof of a correct action.
- Held-out results should be read from the supplied `heldout_test.json` if it
  exists. Its absence means held-out evaluation is pending. Do not infer a
  held-out score from validation.

## Integration checks

1. Review/merge this AI PR and use the newest main worker, not a stale copy.
2. Complete v5 source/launch/staging support and retain legacy v3/v4 behavior.
3. Stage the real bundle with the matching patched runtime and hand12 overlay.
   Existing `tools.stage_model_runner --runner hand12 --bundle <folder> --dry-run`
   is useful for review after its v5 deploy path is updated. This document does
   not authorize replacing a shared running worker without coordination.
4. Compare FPGA outputs with the exported integer reference for both hands;
   verify first-use reload and warm alternating cache slots.
5. Check source identity, independent hands, duplicate suppression, ongoing
   state expiry, reconnect behavior and actual phone/game delivery.
6. Keep this checkpoint labelled provisional while recognition issues remain.
