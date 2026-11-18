# Independent-hand BLE GUI recorder

Adapted from the layout and workflow of
hardware/gloves/181026/debug_model_sample_recording_v5_usb_20261018.py.

## Install and launch (VS Code PowerShell, repository root)

Save hand-ai-gui-cancel-fix.zip in Downloads, then:

```powershell
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\hand-ai-gui-cancel-fix.zip" -DestinationPath . -Force
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_hand_ai*.py" -v
.\.venv\Scripts\python.exe hardware/gloves/181026/debug_model_sample_recording_v6_ble_independent.py
```

Alternative launch: `.\.venv\Scripts\python.exe -m src.hand_ai.record_gui`.
This is an update for the existing repository; it reuses src/model.py, common,
and laptop. It includes current hand_ai modules and both tests, so there is no
need to install the earlier queue-fix ZIP separately. Existing v5 USB recorder,
firmware, datasets and trained models are not replaced.

## Pilot settings

- Hand: RIGHT
- Person: P01
- Session: pilot1
- Split: train
- Gesture: PICKUP
- Number: 3
- Output root: data/hand_pilot

Power RIGHT by USB or battery. Traffic uses BLE. Close other BLE clients connected
to this glove. Select RIGHT and click Connect BLE. Wait for READY and changing
sensor values. Check the displayed receive rate is approximately 50 samples/sec.
Click Start Recording. Each trial has a three-second preparation countdown followed
by a two-second source-timed recording. Reset your hand only in the WAIT period.
The UI updates sensor values continuously; it does not infer a MOVING/IDLE state.
The selected gesture is the ground-truth label you intend to perform.

Files: data/hand_pilot/right/pilot1/P01_pilot1_PICKUP_001.csv and matching JSON.
Existing trials are preserved and the next batch starts after the highest used
trial number. No need to enter a start number. The person/session/hand/split identity
of a folder cannot change. Incomplete trials are discarded on cancellation; saved
completed trials remain. Cancel Recording stops the current batch and keeps BLE
connected so the live display continues. Disconnect BLE closes the connection;
reconnect before recording again. No glove power-off should be necessary for a
normal disconnect. A backend disconnect timeout is reported as unconfirmed closure,
not success. The Windows backend still needs physical validation.

The worker continuously consumes BLE data while waiting, counting down and saving,
so user preparation cannot fill an unconsumed recording queue. Trial data is retained
only during its recording interval. Actual malformed packets, missing sequences,
stale data and trial overflow still fail quality checks. Error messages appear in
the status and log; reconnect after a transport error.

## Full dataset

Once pilot CSVs/values are checked, change output root to data/hand_raw.
Use distinct sessions for splits, with a break/re-wear/recalibration between them:

| Session | Split | Count per gesture |
|---|---|---:|
| trainA | train | 15 |
| trainB | train | 15 |
| valA | validation | 4 |
| testA | test | 6 |

Repeat for PICKUP, DROP, CHOP, WASH, THROW, POURING and IDLE.
This gives 30/4/6 per class, exactly 75/10/15. Save all CSV/JSON pairs together.
Session names alone do not establish independence: actually separate the sessions.
Do not train on the pilot root or use test data to tune the model.

The output schema matches src.hand_ai.pipeline; use the existing prepare/train/
evaluate commands from HAND_AI_GUIDE.md. Two seconds is fixed to match that data
contract. Switching hands requires disconnecting and selecting LEFT. LEFT must
first have its continuous BLE firmware updated and tested; it uses device 1.
Use the same interface for LEFT with its own sessions/data/model folders.
The UI selects one hand per process. Simultaneous two-hand UI/recording is not
claimed tested, and independent classification still needs both hand models.

## Validation

31 automated tests are included. In the authoring environment, 30 passed and the
PyTorch train/evaluate test was skipped because torch is unavailable. It already
passed separately on the user's laptop before this GUI update. New tests cover:
hand selection, invalid settings, auto-numbering, session identity, compatible
CSV/JSON saves, continuous consumption of 3,000 mock samples during preparation,
propagation of reader failures, and a complete mocked recording batch.
The graphical window and physical BLE interaction have not been exercised in this
headless environment. Validate the Windows window and a pilot trial before bulk
collection. No FPGA or AR deployment is changed by this package.

Suggested commit after local tests/pilot pass:

```powershell
git add src/hand_ai hardware/gloves/181026/debug_model_sample_recording_v6_ble_independent.py tests/test_hand_ai.py tests/test_hand_ai_gui.py HAND_AI_GUI_GUIDE.md
git diff --cached --stat
git diff --cached --check
git commit -m "feat: add independent-hand BLE recording GUI"
```

Review staged files first; include other hand_ai files from the initial package
as appropriate. Nothing is pushed automatically.


## Resuming a session

The recording count is the number of NEW trials to record, not the final target.
For the same person, hand, session, gesture and output root, after trials 1-5,
choose count 10 to add trials 6-15. Reconnecting alone does not change a session.
Re-wearing/recalibrating belongs in a new session. pilot1 is practice; use trainA
and data/hand_raw for the real dataset after the pilot passes inspection. Each
gesture is numbered separately. The GUI does not automatically stop at total 15.

## Cancel/disconnect regression tests

The update separates Cancel Recording from Disconnect BLE. Automated mock tests
cover cancelling and restarting a batch without closing BLE, stopping during
connection setup and idle reception, direct disconnect without a prior remote
stop_notify write, cleanup timeout reporting, and rejecting a falsely successful
connected state. Stop requests cancel startup and incomplete batch tasks; completed
files stay saved. These tests do not reproduce a real WinRT stack or certify the
physical issue as fixed. Verify with the powered RIGHT glove after installing.
