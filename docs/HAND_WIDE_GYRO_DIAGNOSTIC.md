# Why the motion-range check is necessary

The six uploaded ±500-sensor WASH trials have valid hashes, source continuity
and timing, with 3 trials per hand and 101–102 samples per trial. They contain
134 gyro axis readings exactly at -327.68/+327.67 deg/s. The previous ±250
sensor ceiling was removed, exposing the existing v4 packet ceiling. This
loses motion information before the AI sees the data. Lowering confirmation
thresholds, retraining or FPGA tuning cannot restore that lost information.

These are 134 **axis readings**, not necessarily 134 distinct timestamps.
This does not prove that a classifier could never work on clipped data; it
shows that avoidable input information is being discarded during natural WASH.

# Opt-in diagnostic format

| Item | Existing v4 | New local diagnostic v5 |
|---|---|---|
| Packet length / channels | 40 bytes / 12 | 40 bytes / 12 |
| Sensor packet version | 4 | 5 |
| Protected source marker | LCS4 | LCS5 |
| Gyro transported scale | 100 | 10 |
| Gyro resolution | 0.01 deg/s | 0.1 deg/s |
| Wire gyro range | -327.68..327.67 deg/s | -3276.8..3276.7 deg/s |
| New environment sensor setting | Earlier default ±250; diagnostic ±500 | ±1000 deg/s |
| Sensor sensitivity | 131 / 65.5 LSB per deg/s | 32.8 LSB per deg/s |

Acceleration, angles, flex channels, source IDs, UUIDs and 50 Hz target remain
the same. Firmware verifies the selected gyro full-scale bits by register
readback before calibrating. This wider sensor setting also avoids merely
moving the clipping from the old wire ceiling to a ±500 sensor ceiling.

The 0.1 deg/s quantization rounds to nearest (ties away from zero) in firmware;
physical conversion divides by 10 once in the v5 BLE adapter. Do not manually
multiply or divide the CSV gyro columns. They are already in physical deg/s.

This is a **local BLE diagnostic path**. Existing v3/v4 decoders explicitly
refuse v5 and the v5 recorder refuses LCS3/LCS4 before subscribing. Production
bridge/server support is not included. That migration needs coordination with
the comms teammate after the physical range check. Compatibility result frames
and phone code are not changed by this package.

The fixed sensor setting is part of the reviewed *-wide build environment.
BLE stats identify v5, not the sensor register itself; serial startup and the
firmware readback check establish the configured range. Do not flash manually
edited variants and assume the same contract.

# Run the next check

Use a new session. Preserve prior diagnostics and the current production board
deployment. Close the old recorder, laptop bridge and serial monitors.
Installing this package does not flash gloves, program the FPGA or push Git.
The installer backs up reviewed files and refuses local conflicts before writes.

From the repository root:

```powershell
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\wide-gyro-diagnostic.zip" `
  -DestinationPath "$env:USERPROFILE\Downloads\wide-gyro-diagnostic" -Force
$kit = "$env:USERPROFILE\Downloads\wide-gyro-diagnostic\run_widegyro.ps1"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step Setup
if ($LASTEXITCODE -ne 0) { throw "Setup failed; send the output before flashing." }
```

Setup runs six focused/regression test files and builds both new firmware targets
using the repository's Python (`python -m platformio`). Windows may skip host
g++ tests. The actual PlatformIO firmware builds remain required.

**Connect only the physical RIGHT glove to USB**, then:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step FlashRight
if ($LASTEXITCODE -ne 0) { throw "RIGHT upload failed." }
```

Keep it motionless on the table for at least 8 seconds after reboot.
Disconnect RIGHT USB, connect only LEFT, then:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step FlashLeft
if ($LASTEXITCODE -ne 0) { throw "LEFT upload failed." }
```

Keep LEFT still for 8 seconds. If several USB development boards must remain
attached, provide the confirmed `-Port COM<number>` to the relevant flash step.
Do not guess hand identity from auto-detected serial port order.

Power both gloves. Any new reboot requires stillness through calibration.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step Record
```

The wide recorder defaults to BOTH / WASH / 3 captures,
session `P01_gyro_wide_check_20261028`, root `data/hand_flex3_checks/wide_gyro`.
Choose Connect selected, then Record selected. Move naturally. Preparation is
2 seconds and capture is 2 seconds. LEFT-only and RIGHT-only modes remain
available and the unselected hand shows Disconnected.

After the batch finishes:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step Audit
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $kit -Step Export
```

Send the new ZIP and matching report JSON from Downloads. For one glove only,
add `-Hands LEFT` or `-Hands RIGHT` to Audit. For a different session, pass its
name through `-Session` on Audit/Export and enter it manually in the GUI.

The audit flags magnitudes >=900 deg/s for review of sensor headroom. This is
a conservative range warning, not proof that the sensor clipped. Exact wire
endpoints and invalid captures block. A pass covers only the observed trials;
it does not establish label correctness, model accuracy or final readiness.

# Recording and training provenance

All twelve physical channels are retained in separate hand files. A shared GO
cue does not synchronize the two ESP32 clocks. Native CSV columns remain the
same, but sessions/trials carry the distinct recording contract
`independent_hand_flex3_gyro_v5_v1` and trial metadata includes the
v5 transport/range/scaling details. Old sessions cannot be appended to under
this new contract. Earlier records and raw backups are not modified.

The existing ordinary v4 training importer intentionally rejects the v5
contract. This package includes an explicit `tools.hand_tonight` importer and
trainer, described in `HAND_TONIGHT_V5.md`. Use that path after the physical
range check; do not rename metadata to v1 or mix diagnostics into the planned
training root. The v5 importer retains original acquisition provenance and
consumes the already-decoded physical values without additional scaling.

The RIGHT index solder fault remains. The planned temporary RIGHT model will
exclude flex1 at training/export while preserving its raw CSV values. This
does not repair the physical connection or protect against faults affecting
power, the IMU or other channels.

# Remaining project checklist

1. Validate wide-range WASH, then the other intended gestures. Check sensor
   and wire headroom, practical glove fit and remaining flex response.
2. Verify the included v5 training importer/trainer on Windows; coordinate
   matching bridge/server and staging support with comms.
3. Record the planned dataset with distinct train/validation/test sessions:
   per person pilot, per gesture per hand, 20/3/4 trials. Seven gestures and
   two hands total 378 hand trials; recording simultaneously gives 189 pairs.
   A different participant cohort needs the planner's rounded counts.
4. Audit identities, hashes, timestamps, gaps, labels, duplicates and split
   separation. Normalize with training data only; keep raw/provenance backups.
5. Train LEFT and RIGHT separately. Use the explicit RIGHT-index exclusion
   temporarily. Compare seeds using validation macro-F1, per-class confusion
   and false-action behavior. Do not select models using the held-out test set.
6. Test continuous recordings and live independent hands, simultaneous actions,
   confidence/confirmation/rearm behavior, response delay and stale input.
7. Export the selected models with matching normalizers/configuration. Check
   numeric precision against actual weights and test both models on FPGA.
   Reuse the current .bit/.hwh only if the hardware/numeric contract still fits.
8. Integrate the matching runner and sensor format with comms. Verify phone/game
   semantics, hand identity, deduplication, ongoing-action expiry and reconnects.
9. Save evaluation evidence, review targeted commits/PRs and merge the chosen
   implementation. Synthetic checks alone do not complete the development phase.

# Rollback and teammate handover

To restore v4, upload the original hand-specific
`firebeetle32-<left|right>-glove-flex3` environment and use `record_both_gui`.
To restore the ±500 diagnostic, use its *-g500 environment instead. Recalibrate
still after every flash. Do not run a v4 bridge against a v5 glove.

Changed firmware files: platformio.ini; comms_packet.h, comms_sample.h and
comms_source_stats.h; both mirrored glove_sensor.cpp copies. New host codec:
common/sensor_v5.py. New local adapter/GUI: wide_ble.py and wide_record_gui.py.
Existing recorder changes add a connection factory and explicit per-session
contract/provenance support; ordinary v4 defaults remain unchanged.

No server, production bridge, game/phone, trained weights or FPGA files are
changed. Do not stage the previously untracked HAND_EVENT transport experiments
with this work. Review small changes and evidence before committing/pushing.

# Research

TDK's MPU-6500 product specification, section 3.1, lists ±1000 deg/s and
32.8 LSB/(deg/s). Its register map, section 4.6, selects that range with
GYRO_FS_SEL=2 in register 0x1B (value 0x10 without other flags):

- https://s3.amazonaws.com/snapeda/datasheet/MPU-6500_TDK_InvenSense.pdf
- https://invensense.tdk.com/wp-content/uploads/2015/02/MPU-6500-Register-Map2.pdf

The wire range/resolution follows directly from signed int16 / scale 10.
The ±1000 choice is an engineering starting point for this observed motion,
not a claim that all gestures are guaranteed to remain in range.
