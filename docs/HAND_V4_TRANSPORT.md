# Opt-in v4 transport activation: laptop to model callback

This patch wires twelve-feature sensor transport into the EXISTING laptop
bridge, protected source audit, command codec, server validator, model port and
model bridge. Both laptop and server still default to v3. It does not activate
HAND_EVENT forwarding or change a deployed service, FPGA overlay or phone app.

## Install and test without gloves

The download contains complete updated files plus a guarded installer. Extract
it into Downloads, then invoke install_hand_v4.py from the project directory.
The installer checks all target files before writing anything. It accepts the
reviewed base version (with CRLF/LF normalization) or the already-installed
updated version. Different local comms edits cause a refusal with filenames.
Do not force an overwrite in that case; review/merge those edits first.

Original files are backed up under .comms-local/backups/hand_v4_transport_... .
The installer changes code only: it starts no service and commits/pushes nothing.
An optional --check performs only the preflight. Reinstallation is idempotent.

```powershell
.\.venv\Scripts\python.exe -m unittest discover `
    -s tests -p "test_hand_v4_transport.py" -v

.\.venv\Scripts\python.exe -m unittest discover `
    -s tests -p "test_bridge.py" -v

.\.venv\Scripts\python.exe -m tools.hand_v4_transport_check `
    --out .comms-local/hand_contract/transport_v4_v1.json
```

The new suite has 11 checks. The existing bridge suite has 17. The loopback
TLS check uses temporary certificates from tools.generate_comms_pki, verifies
the CA and hostname, uses two random local ports and removes its temporary PKI
afterwards. It sends two synthetic packets for each version through actual
laptop Bridge.forward_one() and CommsServer instances, verifies ACKs and model
physical values, and confirms that the other packet version is rejected.
No SSH tunnel, BLE radio, FPGA or phone is exercised by this check.

Temporary PKI generation requires cryptography. If missing:

```powershell
.\.venv\Scripts\python.exe -m pip install cryptography
```

The runtime itself does not gain a cryptography dependency. Existing BLE tests
need the project's Bleak installation. The broader existing pytest suites need
requirements-dev.txt. Before merge, also run the relevant regression suites:

```powershell
.\.venv\Scripts\python.exe -m pytest `
    tests/test_sensor.py tests/test_protocol_rules.py tests/test_source_audit.py `
    tests/test_goodput.py tests/test_laptop_controls.py tests/test_model_interface.py `
    tests/test_model_bridge.py tests/test_transport.py -q
```

Local preparation validation: all 11 new checks and the real loopback TLS probe
passed. The existing bridge suite passed 15 checks here; two BLE lifecycle tests
were blocked by the absent Bleak package. pytest was also unavailable in the
preparation environment. Complete those regressions on the normal project
environment before merging/deploying. Do not treat missing dependencies as
successful tests or disable the tests to obtain a green result.

## Exact code changes for comms handover

| File | Change |
| --- | --- |
| common/sensor_v4.py | New explicit 40-byte/twelve-channel codec with separate synthetic fixtures. |
| common/control.py | Optional sensor_version selects the command's embedded sensor codec and transform length; control framing stays B7/version 1. |
| laptop/bridge.py | BridgeConfig.sensor_version defaults to 3; mode 4 selects all packet parsing, fixture generation, MTU checks, stats parsing and counters consistently. Adds CLI flags --sensor-version, --device, --source-audit, --controls, --source-rate. |
| laptop/source_audit.py | Optional sensor_version; default LCS3, explicit v4 expects LCS4. Same 36-byte counter layout and identity checks. |
| laptop/controls.py | Optional sensor_version selects command payload encoding/decoding and expected transformation; rate/file controls retain their framing. |
| laptop/goodput.py | Optional sensor_version accepts the right packet type and counts 40 bytes in v4, 38 bytes in v3. |
| ultra96/protocol.py | Explicit sensor_version selects data message versions and required channel counts. Default exact v3 schemas remain. Subscription/monitor envelopes remain version 1. |
| ultra96/model.py | Optional physical converter in ModelPort; default is unchanged, v4 uses twelve channels once. |
| ultra96/model_bridge.py | Configure selected channels before start; mode changes are refused after connection/listener startup. Observation hello remains format 1 with the selected channel names. |
| ultra96/server.py | --sensor-version 3/4, default 3. Passes the selected codec to ingestion validation, model conversion, data results and bridge configuration. |
| tools/hand_v4_transport_check.py | Real localhost TLS diagnostic using synthetic inputs and scripted results. |
| tests/test_hand_v4_transport.py | Version isolation, stats, scaling, commands, goodput, model hello and actual TLS tests. |

No firmware, AI recording/training code, existing common.sensor v3 codec,
production certificates, SSH users, phone code or FPGA artifacts are changed.
The installer and this guide are delivery/support files.

## Activation after regression tests and team coordination

- Set BridgeConfig(sensor_version=4) or laptop.bridge --sensor-version 4.
- Start a separate test server with ultra96.server --sensor-version 4.
- Select --model ultra96.model_bridge to feed the independent-hand observer.
  Configure COMMS_MODEL_SOCKET to a NEW isolated path rather than replacing
  the shared paired-model runner socket. Observation hello is format 1 plus
  twelve channels, which src.socket_worker_hand already understands.
- Use alternate local/remote ports and the user's own SSH identity. The normal
  demo launcher/dual supervisor has not been changed to propagate this option;
  do not assume demo.py automatically enables v4 after installing this patch.
- Physical BLE validation must use the correct device ID, authenticated bond,
  LCS4 and compatible SEND_ALL firmware. The bridge API supports audit/rate
  controls. CLI options --controls --source-audit --device N --source-rate 50
  select them explicitly; do not use --diagnostic-unprotected for normal capture.
- An opt-in v4 server still emits LEGACY-SHAPED GESTURE_RESULT envelopes with
  v=4. The current v3-only phone parser needs a matching explicit update before
  connecting it. These are not the structured HAND_EVENT messages.

## Remaining work

The independent-hand worker remains an observer: no action events are sent to
gameplay. The proposed HAND_EVENT contract, EventEncoder and HandAdmission are
separate components. Next integrate a structured event callback through model
port/bridge/server and update phone parsing/dedup/leases/resync together. Do not
squeeze state_start/state_end into the old five-field result callback, which
would discard metadata or collide on a shared source sample.

Real recordings, trained-model quantization checks, a matching bit/HWH and board
tests remain separate. This transport test establishes channel preservation and
version isolation; it does not establish gesture accuracy, 50-Hz radio goodput,
full end-to-end latency or gameplay correctness.
