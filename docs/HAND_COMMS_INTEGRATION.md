# Independent-hand communications: contract and reference adapters

Status: locally tested reference implementation, not activated in the shared
service or phone. This checkpoint works without gloves, Vitis, a board, trained
models or a phone. It adds six files and does not replace production modules.

## What changed in this checkpoint

| New file | Purpose |
| --- | --- |
| common/hand_contract.py | Strict sensor-v4 codec/network validation and proposed versioned event schemas. |
| src/hand_events.py | Converts existing local runtime records into complete event envelopes; reference receiver with duplicate suppression and state leases. |
| ultra96/hand_admission.py | Server-side helper checking that prediction events name exact, recently accepted source samples. |
| tools/hand_contract_demo.py | Offline framing/adapter/consumer demonstration with scripted model scores and injected faults. |
| tests/test_hand_contract.py | Codec, source identity, event ordering, leases and integration checks. |
| docs/HAND_COMMS_INTEGRATION.md | This handover and activation checklist. |

The existing common.sensor, laptop.bridge, ultra96.protocol, ultra96.model,
ultra96.model_bridge and ultra96.server are unchanged. Their defaults still
use sensor-v3/eleven-channel and legacy gesture-result behavior. The new helpers
are in the common/ultra96 namespaces so comms can use them directly in the
coordinated migration; dropping these files into the repo alone does not switch
the running service. No teammate messages or repository pushes were sent.

## Why this contract is needed

The current pipeline has several separate compatibility points:

- laptop/bridge.py imports the shared v3 sensor codec.
- laptop/source_audit.py expects source-statistics marker LCS3; v4 uses LCS4.
- ultra96/protocol.py validates exact fields, v3 and eleven raw sensor values.
- ultra96/model.py converts values using the v3 physical-unit converter, and
  offers only emit(gesture, confidence, device_id, boot_id, seq).
- ultra96/model_bridge.py forwards only those five result fields.
- ultra96/server.py de-duplicates results by device:boot:seq, allowing only
  one result per source sample.

A CHOP-to-POURING transition can produce two events from one source sample:
end CHOP and start POURING. Both are meaningful. The old result ID would collide,
and adding extra JSON fields alone would not work because the bridge discards
them and the network schema rejects them.

## 1. Run on Windows

Stay on AI-fpga-independent-hand. Extract the ZIP into the repository root.
It requires the independent-hand runtime already added on this branch and the
existing common.wire framing module. It does not require Unix sockets.

```powershell
.\.venv\Scripts\python.exe -m unittest discover `
    -s tests -p "test_hand_contract.py" -v

.\.venv\Scripts\python.exe -m tools.hand_contract_demo `
    --out .comms-local/hand_contract/demo_v1
```

Expected: 28 tests pass; demo summary reports 442 sensor frames, 442 bridge
samples, 7 event frames, 1 accepted pulse, 1 ignored duplicate, 2 distinct
transition event IDs and 1 lease expiry after a deliberately dropped stop.
Both final active states are null. Use a new output directory for repeat runs.

The demo writes hello.json, sensor_frames.jsonl, bridge_samples.jsonl,
hand_events.jsonl and summary.json. It uses the EXISTING four-byte length-prefix
framing functions in memory. It does not exercise TLS, BLE radio, the real
server, FPGA inference, a phone or gesture accuracy. Its prediction scores are
scripted specifically to test action handling.

## 2. Sensor/ingestion contract

- Sensor packet: LC magic, version 4, device ID, boot ID, sequence, uptime,
  twelve signed int16 values; 40 bytes, little endian.
- One BLE notification contains 1 to 6 complete packets. v3, malformed batches,
  missing values and padding/truncation are refused.
- Feature order: ax, ay, az, gx, gy, gz, yaw, pitch, roll, flex1, flex2, flex3.
- Scales: acceleration /10000, gyroscope /100, angles /100, flex /10000.
- Preserve index GPIO34, middle GPIO35, thumb GPIO39; LEFT=1 and RIGHT=2.
- Network SENSOR_BATCH retains the current envelope fields but uses v=4 and
  twelve RAW int16 values. Convert to physical units exactly once at the model
  boundary. to_bridge_sample() produces the twelve-float sample object.
- request_id is null for AI samples. Control-command packets remain distinct;
  the helper refuses forwarding a command into model sampling.
- 50 samples/second per hand remains the target; the model input is a separate
  two-second window resampled to 50 time positions per hand.

The codec is an opt-in reference and has a byte-compatibility test against the
existing src.hand_ai_flex3.sensor recorder codec. Consolidate duplicate codec
implementations only after migration, regression tests and hardware validation.

## 3. Worker handshake proposal

The event-aware bridge negotiates hello format=2, sensor_version=4,
event_format=1, twelve channels, seven ordered class names, session_id,
a fresh worker_epoch (32 lowercase hex), lease_ms, confidence_threshold and
confirmations. Initial settings: 1000 ms lease, confidence strictly above 0.70,
three confirmations. Settings are engineering starting points, not course rules.

The bridge assigns a fresh connection epoch on each runner connection. Both
ends validate capabilities before accepting events. The current observer worker
still accepts format 1/twelve channels for observation only; adding this new
format 2 handshake and event transmission is an explicit activation task.
Old format-1/eleven-channel runners are not silently upgraded.

## 4. Event envelope and meanings

Every event carries these exact fields:

```json
{
  "v": 4,
  "type": "HAND_EVENT",
  "event_format": 1,
  "session_id": "ltc-comms",
  "worker_epoch": "1234567890abcdef1234567890abcdef",
  "device_id": 1,
  "event_seq": 1,
  "event_id": "1234567890abcdef1234567890abcdef:1:1",
  "kind": "state_start",
  "gesture": "CHOP",
  "confidence": 0.95,
  "source": {"boot_id": 11, "seq": 120, "uptime_ms": 2400},
  "state_id": "1234567890abcdef1234567890abcdef:1:1",
  "lease_ms": 1000,
  "reason": "confirmed"
}
```

| kind | Meaning |
| --- | --- |
| pulse | One accepted PICKUP, DROP or THROW request. Gameplay checks validity. |
| state_start | Begin confirmed CHOP, WASH or POURING. Its event_id becomes state_id. |
| state_refresh | A fresh above-threshold prediction maintains that same state; initial refresh cadence 0.2 s. |
| state_end | End the named state, including timeout/reset. Confidence is null; no fabricated IDLE. |
| idle | The model confidently confirmed IDLE. |
| reset | Lifecycle reset; gesture/confidence are null. It is not an AI prediction. |

Counters are independent per hand, ordered and bounded to JavaScript-safe
integers (1..2^53-1). They do not wrap within an epoch. Two events may refer to
the SAME source sample while retaining different event IDs. Transport retries
must resend the exact serialized event/ID rather than minting another ID.

The reference consumer ignores duplicate/out-of-order events, retains separate
hand states, and expires ongoing states on its OWN monotonic clock. Retransmitted
refresh events do not extend leases. An old state's end cannot clear a newer
state. Unknown/expired-state refreshes cannot resurrect actions. After a dropped
start or lease expiry, production must request a fresh state start/resync before
continuation; this request/response transport is not implemented here.

This is bounded duplicate suppression, not a promise of exactly-once delivery
across crashes/restarts or disconnected game clients. Delivery acknowledgements,
recovery and persistent gameplay effects require a team decision. A pulse
received by the reference consumer is a request, not execution in the game.

## 5. Server admission rules

HandAdmission.accepted_sample() keeps a bounded exact-source ledger per hand.
Duplicates do not make old samples appear fresh. Changed bytes under the same
source identity are refused, and retired boots cannot become current again.
Sequence wrap is supported; source namespaces are bounded for the session.

HandAdmission.event() validates session, connection epoch and the exact accepted
boot/seq/uptime identity. Prediction events must refer to the current boot and a
source accepted within 0.5 s at the server. Lifecycle ends can reference an older
accepted sample because no new sensor packet is required to report a timeout.
The consumer's state_id check protects unrelated states. If an old source has
left the bounded ledger, reject it; lease expiry remains the fallback.

The admission helper does not itself publish, acknowledge or mutate game state.
The service must use it on the event-loop thread and forward the complete event
envelope rather than routing through the old five-argument emit() path.
Server freshness is measured from server acceptance, not glove generation;
cross-machine clocks and end-to-end network delay still require measurement.

## 6. Coordinated activation checklist for comms/game

1. Add explicit v4 mode to laptop decoding/source-audit and server ingestion,
   including LCS4 checks, 40-byte MTU assumptions and twelve-value conversion.
   Keep packet versions, ingestion ACKs and any dummy/control fixtures aligned.
2. Keep TLS authentication, SSH forwarding, BLE authentication, bounded queues,
   counters and freshness checks. This migration does not bypass them.
3. Add a dedicated structured event callback/port to ultra96.model/ModelPort,
   model_bridge and server. Preserve all HAND_EVENT fields and exact source IDs.
4. Negotiate hello format 2 and update the worker to encode and transmit local
   events with EventEncoder. Forward refreshes/lifecycle ends as well as pulses.
5. Use event_id deduplication, not one-result-per-source-sample deduplication.
   Preserve both events in a transition. Record queue drops and enforce source
   freshness before publishing prediction events.
6. Add phone/game parsing for HAND_EVENT, independent per-hand state, lease
   timers, deduplication, reconnect resets and state resynchronization. UI and
   monitor consumers may also need updated message handling.
7. Run isolated service tests on alternate ports and local test consumers before
   changing the shared deployment. Test disconnects, duplicates, overload and
   missing stop events. Then validate with the real gloves/model/FPGA/phone.

No group messages were sent. Review this contract with comms and game before
activating it; agreement on schema does not establish real-model accuracy or
physical/end-to-end validation.
