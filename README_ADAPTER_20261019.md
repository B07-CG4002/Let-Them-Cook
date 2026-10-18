# Connect the AI worker to the production comms server

## Files to add

Copy `src/comms_adapter.py` into the repository's existing `src/` folder.
Copy `tests/test_comms_adapter.py` into the existing `tests/` folder.
Do not replace `ultra96/model.py` or `ultra96/server.py`.

The adapter uses only Python's standard library. The normal-user comms service
imports it; your separate PYNQ worker still runs under its own Python/sudo.
The adapter DOES NOT launch the worker or load a model/bitstream.

## Behaviour

- `start(emit)` creates the Unix socket before returning and starts forwarding threads.
- `on_sample(...)` only queues samples; it does not wait for inference or socket writes.
- Sends the worker its existing `hello` and newline-JSON `sample` messages.
- Receives `result` messages and invokes the comms server's thread-safe `emit(...)`.
- Buffers at most128 samples (combined streams); drops oldest on overflow.
- Drops samples held in its own queue for more than100ms.
- Drops samples when no worker is connected; it does not backfill old data.
- Accepts results only for identities forwarded to that connection; refuses a second
  result with the same identity. The comms server independently validates results.
- Does not rename, threshold, merge or debounce model labels.
- Socket permissions0600; parent created0700 if absent. It never replaces an existing
  socket/file automatically. The worker runs as the same user or root.
- Clean process exit removes its owned socket. Force-killing the service may leave it.
  If startup reports an existing socket, check its owner/listener before removing it.
- Reconnect clears queues and identity history. Service startup does not wait for a
  worker, so service 'model_ready' means the adapter is ready, not FPGA inference ready.

## 1. Copy to Ultra96 — laptop PowerShell, repository root

```powershell
ssh -J ooiwenre@stujump.comp.nus.edu.sg xilinx@makerslab-fpga-35.ddns.comp.nus.edu.sg "mkdir -p /tmp/cnn_conv1d/ai_integration/src /tmp/cnn_conv1d/ai_integration/tests"

scp -J ooiwenre@stujump.comp.nus.edu.sg src/comms_adapter.py xilinx@makerslab-fpga-35.ddns.comp.nus.edu.sg:/tmp/cnn_conv1d/ai_integration/src/

scp -J ooiwenre@stujump.comp.nus.edu.sg tests/test_comms_adapter.py xilinx@makerslab-fpga-35.ddns.comp.nus.edu.sg:/tmp/cnn_conv1d/ai_integration/tests/
```

These commands add files only. They do not stop or reconfigure Jie Yan's service.

## 2. Test — Ultra96 SSH

```bash
cd /tmp/cnn_conv1d/ai_integration
/usr/bin/python3 -m unittest discover -s tests -p test_comms_adapter.py -v
```

Expected: four tests pass, including the socket round trip. This test uses a
separate temporary socket and a fake worker message, not the production socket,
real model or FPGA. It verifies the adapter connection and callback.

Preparation checks: three non-socket tests passed; socket test skipped because
Unix socket creation is blocked in the preparation environment. Run it on board.

## 3. Configure the real server — coordinate with Jie Yan

He should retain his existing certificate, key, session ID, ports and other service
options. Add these options to his server configuration:

```text
--model src.comms_adapter --model-path /tmp/cnn_conv1d/ai_integration
```

His supplied `tools/deploy_service.py` supports both options but copies only
`common/` and `ultra96/`, so it does NOT upload this adapter. Copying it separately
in step1 is required. The absolute `--model-path` points to the directory containing
`src/`. His deployment rollback also does not snapshot external AI files.
Use a durable, versioned deployment directory instead of /tmp for the final setup.

Optional read-only deployment-plan preview from LAPTOP PowerShell:

```powershell
python -m tools.deploy_service --dry-run --jump-user ooiwenre --model src.comms_adapter --model-path /tmp/cnn_conv1d/ai_integration
```

Do not run an actual redeploy/restart until coordinated with Jie Yan. His launcher
uses the most recent deployment record to preserve model options. Starting the
ordinary dummy service will not create this AI socket.

Default socket:
`/var/tmp/cg4002-week7-yanjie-20260907/model-runner.sock`

It can be overridden for the service with `CG4002_AI_SOCKET`; the worker must use
an identical `--socket` value. No override is needed for the default workflow.

## 4. Start AI worker — Ultra96 SSH, after adapter service starts

Stop any previous replay/AI worker first. Do not start two FPGA workers.

```bash
cd /tmp/cnn_conv1d/ai_integration
sudo -E /usr/local/share/pynq-venv/bin/python -m src.socket_worker \
  --weights fpga/hls/eightclass_weights_20261018_v2.npz \
  --bit /tmp/cnn_conv1d/cnn_conv1d_fixed28_v2.bit
```

This uses the default confidence threshold0.70, not the zero threshold used during
replay wiring tests. The threshold is provisional. Result repetition, continuous
window errors, acquisition-time alignment and IDLE policy remain to validate.

A successful production test needs actual glove input, model result metrics and
phone/game observation. Adapter unit-test success alone does not establish that.

## Known integration issues to settle

1. Both gloves must send continuous samples, including idle, for the current
   paired two-second worker. Movement-only transmission can prevent valid windows.
2. New labels pass the comms protocol's generic label validation. The AR/game still
   needs to map PICKUP, DROP, CHOP, WASH, THROW, FIREEXT, POURING and IDLE.
3. Repeated confident CHOP outputs currently represent repeated windows; decide
   whether AR should trigger one action, repeat at a controlled rate, or await a
   transition. Do not assume unique result IDs solve repeated physical actions.
4. Worker receive times are approximate. Adapter queueing and transport batching
   can distort timing; this adapter does not synchronize glove acquisition clocks.
5. Hybrid fixed-input checks passed24/24 class agreement and87.5% test accuracy in
   the user's earlier board run. This adapter is NEW and still needs its own board
   socket and production-service checks.

## Commit and PR

Commit:
`:star: feat(ai): bridge comms model API to socket worker`

PR title:
`feat(ai): connect comms model interface to hybrid inference worker`

PR description:
Add a standard-library adapter exposing start(emit) and on_sample(...), forwarding
samples to the separate AI worker through its existing Unix-socket contract.
Keep sample intake nonblocking with a bounded freshness queue and validate unique
result identities before calling emit. Include adapter tests and deployment notes.
Local validation: three tests passed; Unix socket test requires Ultra96 execution.
Production service and live-glove validation remain pending.
