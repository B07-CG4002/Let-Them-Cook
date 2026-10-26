# Independent-hand runtime: offline and observation checkpoint

This package adds an integer NumPy reference, a NEW-IP PYNQ driver, independent
source-time windows, local action confirmation and a diagnostic bridge observer.
It can be tested on Windows without gloves, a board, Vitis or trained models.
It depends on the hand HLS generator from the previous checkpoint and the
existing src.hand_ai_flex3.core module. It adds six new repository files.

## Scope and integration status

- Replay uses synthetic physical-unit samples and two synthetic parameter sets.
- The integer reference matches the eight exact reference vectors already used
  in the independent-hand C/RTL test.
- The PYNQ driver is tested with a mocked IP/buffers/register map. It has NOT
  been validated on a physical board. A compatible hand bit/HWH is not built yet.
- Socket mode validates the existing bridge format 1 with TWELVE channel names.
  The current main-branch common.sensor and deployed bridge still use eleven
  channels/v3, so they will be rejected. This package does not modify them.
- Production socket mode uses Unix-domain IPC on Ultra96/Linux. Some Windows
  Python builds have no AF_UNIX; use replay on Windows. Socket tests inject an
  already-connected portable socket pair and deliberately simulate no AF_UNIX.
  They still exercise real reads, thread shutdown and the no-game-message rule.
- No messages are sent back to the bridge or game. Action events are LOCAL
  diagnostics, including explicit state_end and timeout reasons. The current
  bridge accepts only gesture/confidence/source identity and would discard the
  event/state metadata. Gameplay transport requires a coordinated later change.
- No running deployment, FPGA overlay, firmware or model is changed by the
  Windows commands below. Synthetic data is not a gesture-accuracy evaluation.

## Files added

- src/hand_runtime.py: bundle verification, two independent source-time windows
  and local confirmation/state handling.
- src/hand_backend.py: integer NumPy reference and the new PYNQ driver.
- src/socket_worker_hand.py: replay and socket observation; JSONL diagnostics.
- tools/hand_worker_demo.py: deterministic six-second replay, 50 Hz per hand.
- tests/test_hand_runtime.py: runtime, mocked hardware and socket tests.
- docs/INDEPENDENT_HAND_RUNTIME.md: this guide.

## 1. Test on Windows

Stay on AI-fpga-independent-hand. Extract the ZIP into the repository root.
Use .\.venv\Scripts\python.exe directly; no environment activation is needed.

```powershell
.\.venv\Scripts\python.exe -m unittest discover `
    -s tests -p "test_hand_runtime.py" -v
```

The tests do not require PyTorch, PYNQ, g++, BLE or real hardware. They create
temporary synthetic bundles and use the existing generator/read-only HLS
sources. They cover golden integer words, per-hand normalization and routing,
uint32 wrap, duplicates, source gaps/reboots, angle wrap, one-hand operation,
strict confidence threshold, one-shot rearming, ongoing-state expiry, stale
inference suppression, changed artifact hashes, 64-bit pointers, HWH-derived
register offsets, hardware return errors/timeouts, and actual socket-pair I/O.

## 2. Run the synthetic replay

Reuse .comms-local/hand_hls/synthetic12_v1 from the already completed HLS test.
Do not regenerate or overwrite that experiment.

```powershell
.\.venv\Scripts\python.exe -m tools.hand_worker_demo `
    --out .comms-local/hand_runtime/demo12_v1.jsonl

.\.venv\Scripts\python.exe -m src.socket_worker_hand `
    --bundle .comms-local/hand_hls/synthetic12_v1 `
    --allow-synthetic `
    --replay .comms-local/hand_runtime/demo12_v1.jsonl `
    --backend numpy `
    --out .comms-local/hand_runtime/worker12_v1.jsonl
```

Expected: a summary with 42 prediction records, 21 LEFT and 21 RIGHT.
These arbitrary synthetic weights normally stay BELOW 0.70 confidence, so no
accepted action events are expected. This is intentional; do not lower the
threshold to manufacture successful gesture recognition. Unit tests separately
exercise confirmation/state transitions using explicit high-confidence fixtures.

Both input and output files refuse overwrites. For a deliberate repeated run,
choose new names such as demo12_v2.jsonl and worker12_v2.jsonl.

Raw predictions, including low-confidence predictions, are retained in the
JSONL file for investigation. Console output displays accepted local events
and the summary. NumPy processing timings are NOT FPGA inference timings.

## Windows, identity and timing rules

Each hand has its own two-second window resampled to 50 positions. The other
hand can be absent. Source uptime determines sampling positions; notification
or network arrival timing does not stretch/compress the gesture. Angles use the
same unwrap/interpolate/wrap preprocessing as recording/training.

Boot changes, sequence gaps, source timing gaps over 100 ms, or host receive
silence over 500 ms reset only the affected hand. Duplicate/out-of-order packets
do not advance confirmation. uint32 seq/uptime wrap is supported. A source reset
during inference invalidates its result. Results older than 500 ms since worker
receipt are suppressed. No inference queue of old windows is accumulated.

The receiver runs separately while a single inference loop services both hands.
Scheduling starts the next interval from inference selection, rather than adding
an extra 200 ms wait after inference completes. Slow computations can still
cause missed deadlines; the worker uses fresh data rather than catching up on
old windows. Hardware calls share one engine and are serialized by the driver.

worker_sample_age_ms measures time since THIS worker received the sample.
It is not Bluetooth-to-phone latency. Source, laptop, server and phone clocks
are not synchronized by this package. Correlate device_id/boot_id/seq across
stages and use a measured clock-alignment strategy for full transport latency.

## Local action handling

- Confidence must be STRICTLY above 0.70 by default.
- Three consecutive qualifying predictions of the same label are required.
  At a 0.2-second interval this spans approximately 0.4 s from the first one.
- PICKUP, DROP and THROW emit a one-shot action. Holding the same prediction
  does not repeat it, including across low-confidence flicker. A confirmed IDLE
  or another confirmed label permits a subsequent transition/action.
- CHOP, WASH and POURING emit state_start and later state_end events locally.
- An ongoing state expires if its matching high-confidence prediction is not
  refreshed for 0.6 s. Other labels cannot keep that old state alive.
- Low confidence is unknown, not IDLE. Timeout ends a local state without
  claiming that the model predicted IDLE.
- Reboot/gap/disconnect clears the affected state; a socket disconnect resets
  both connection-associated streams. Hand confirmation/latches remain separate.

The 0.70 threshold, three confirmations and 0.6 s state timeout are initial
engineering settings. Tune them on validation/live recordings, not the test set.
The model may still make a stable wrong prediction. Confirmation does not fix
sensor placement, training/live mismatch or persistent class confusion.

## New FPGA driver contract (future board checkpoint)

The driver loads only when --backend fpga is explicitly selected and a NEW
matching --bit and same-name .hwh are supplied. An incompatible bridge hello
is rejected before backend/overlay initialization. Do not run FPGA mode against
the current shared production deployment or its old paired bitstream.

The driver verifies exported artifact hashes, shape/format/class order and each
hand's normalizer. It normalizes raw physical-unit windows before encoding
A32/F18 inputs. Normalized values outside the representable range raise an
error rather than silently saturating.

The new top/IP name is gesture_cnn_hand_u2_cache / gesture_cnn_hand_u2_cache_0.
Pointer/control/return offsets are read from HWH. CTRL or AP_CTRL and ap_return
or return must be present, along with the three cache controls and 64-bit
pointers. Unexpected HWH metadata raises a descriptive error; inspect the real
register map before adapting a driver. Old paired register offsets are not used.

Each parameter buffer is allocated/flushed once. First use of each hand forces
a reload into its slot, then AUTO permits hits. Return statuses are checked.
Input setup, start/poll, output read, input encoding and softmax timings are
recorded separately. Parameter caching does not cache normalization or windows;
those remain independent CPU state.

On timeout/error the driver stops further calls. A busy IP retains its buffers
until the hardware has been stopped/reset; there is no automatic overlay reset
or retry. Hardware commands and lifecycle still need controlled board testing.
The bit/HWH hashes are recorded at startup for deployment evidence.

## Bridge and gameplay work still required

Coordinate v4/40-byte/twelve-channel transport through laptop, network protocol,
Ultra96 server and model bridge. Preserve LEFT=1/RIGHT=2 and physical feature
scales. Do not pad old eleven-feature packets or reuse an old model to pretend
the thumb is present.

Agree how to carry one-shot event IDs, state starts/ends, expiry, source identity
and reconnect resets into gameplay. The current bridge strips additional fields,
so merely adding event_kind to its old result JSON is insufficient. Gameplay
then checks ingredient/object ownership, location and action validity, and
controls repetition for ongoing states. This package deliberately logs these
local events without forwarding them.

After real recordings: train/evaluate each hand, export both trained models,
validate actual quantization/confidence behavior, build/validate bit+HWH, test
the new driver on the board, then enable end-to-end gameplay transport.

References for implementation review:
- https://docs.amd.com/r/en-US/ug1399-vitis-hls/S_AXILITE-Control-Register-Map
- https://pynq.readthedocs.io/en/latest/pynq_libraries/allocate.html
