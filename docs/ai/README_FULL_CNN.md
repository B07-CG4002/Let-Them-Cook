# Full CNN FPGA deployment

## Status

The eight-class CNN has passed an Ultra96 board test:
- Exact fixed-point output mismatches: 0.
- Agreement with software predictions: 24/24.
- Classification accuracy: 21/24 (87.5%), matching software.
- Mean measured accelerator call: 2.044 ms.
  This excludes input packing/flush and model/overlay loading.
- Vivado implementation met setup, hold and pulse-width timing constraints.

Live glove-to-phone integration with this full-CNN worker remains to be verified.

## Execution

The FPGA runs both convolutions, ReLU activations, pooling and fully
connected layers. The Ultra96 CPU prepares inputs, controls the FPGA,
and calculates the final class and softmax confidence.

Classes, in model order:
PICKUP, DROP, CHOP, WASH, THROW, FIREEXT, POURING, IDLE.

## Required files

Run from a repository checkout containing:
- src/ai_common.py
- src/export_full_cnn.py
- src/full_cnn_backend.py
- src/socket_worker_full.py
- src/test_full_cnn_board.py
- src/comms_adapter.py
- fpga/hls/eightclass_weights_20261018_v2.npz
- fpga/overlays/full_cnn/cnn_full_20261020.bit
- fpga/overlays/full_cnn/cnn_full_20261020.hwh

Keep the bitstream and HWH together with identical basenames.
Use the board's PYNQ Python environment with NumPy and PYNQ installed.

## Start integration

Stop the previous inference worker before starting this worker.
Loading an overlay replaces the current FPGA configuration.

Start the communications server with the existing AI socket adapter.
Then, from the repository root on Ultra96:

```bash
sudo -E /usr/local/share/pynq-venv/bin/python -u -m src.socket_worker_full \
  --weights fpga/hls/eightclass_weights_20261018_v2.npz \
  --bit fpga/overlays/full_cnn/cnn_full_20261020.bit \
  --socket /var/tmp/cg4002-week7-yanjie-20260907/model-runner.sock \
  --threshold 0.70 \
  --interval 0.20
```

The server/adapter creates the socket; the inference worker connects to it.
The socket path must match on both sides.

## Inputs and outputs

The worker receives separate left/right glove streams:
- device_id 1: left; device_id 2: right.
- 11 values per glove:
  ax, ay, az, gx, gy, gz, yaw, pitch, roll, flex1, flex2.
- Units: g, degrees/second, degrees, and flex values from 0 to 1.
- Target rate: 50 samples/second per glove.

Both gloves contribute to one action prediction.
The worker forms two-second windows and resamples to 50 time positions
with 22 combined features. Receive-time alignment is approximate and
must be checked under actual network delays.

Inference attempts are spaced by at least 0.20 seconds.
Results below 0.70 confidence are suppressed, not renamed IDLE.
IDLE is emitted when the model predicts IDLE above threshold.
Repeated predictions can produce repeated results.

Results contain:
type, gesture, confidence, device_id, boot_id, seq.

The sample identity identifies a contributing sample; it does not identify
which hand performed the action.

## Deployment and fallback

Deploy code, weights and overlay files into the persistent deployment
directory. Files currently under /tmp are temporary.

The previous src.socket_worker with cnn_conv1d_fixed28_v2.bit is the hybrid
fallback: FPGA Conv1 with remaining layers on CPU.
Restart the selected worker to load its corresponding overlay.
Never run the hybrid and full-CNN workers concurrently.