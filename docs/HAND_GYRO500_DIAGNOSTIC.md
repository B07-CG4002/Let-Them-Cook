# Diagnostic gyro-range check

The table-stationary recordings showed good bias calibration. Earlier WASH
recordings repeatedly reached the MPU-6500's ±250 deg/s sensor limits. A larger
sensor range is the next diagnostic, rather than another stationary repeat.

## What changes

`GLOVE_GYRO_RANGE_DPS=500` selects register 0x1B value 0x08 and 65.5 LSB/(deg/s).
The default remains 250 with register 0x00 and 131 LSB/(deg/s). The driver checks
the configured full-scale bits by I2C readback before calibrating. Both copies
of `glove_sensor.cpp` stay identical for the existing sync workflow.

These two new environments explicitly select ±500:

- `firebeetle32-left-glove-flex3-g500`
- `firebeetle32-right-glove-flex3-g500`

Both inherit the real three-flex glove environments: IDLE included, 50 Hz,
same hand IDs, same BLE addresses and v4 packets. Ordinary environments are
unchanged. The recorder still receives physical deg/s. No renormalization or
manual multiplication of recorded gyro values is required.

**The existing wire limit remains -327.68..327.67 deg/s.** This experiment tests
whether natural gestures fit within that range after removing sensor clipping.
It does not claim that all of the sensor's ±500 range can reach the recorder.
At startup serial reports `gyro_config range_dps=500` and sensitivity 65.5.
BLE v4 does not advertise the IMU range, so the offline audit's declared range
must be checked against the actual flashed environment/startup message.

The existing embedded MOVING/IDLE classifier was trained with older data. Its
classification may change with the wider range. These diagnostic environments
retain SEND_ALL, so that classifier must not be used to gate diagnostic capture.

## 1. Branch, install, test and build

Preserve existing work before switching branches. Do not stage unrelated
experimental HAND_EVENT transport files. The installer preflights all files,
backs up changed existing files under `.comms-local/backups/gyro500_<time>` and
refuses conflicting edits. It accepts Windows CRLF. It does not flash anything.

```powershell
git switch -c AI-gyro500-diagnostic

Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\gyro500-diagnostic.zip" `
  -DestinationPath "$env:USERPROFILE\Downloads\gyro500-diagnostic" -Force

.\.venv\Scripts\python.exe `
  "$env:USERPROFILE\Downloads\gyro500-diagnostic\install_gyro500.py" --repo .
if ($LASTEXITCODE -ne 0) { throw "Installer stopped; send the output." }

.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_gyro_trial_audit.py" -v
if ($LASTEXITCODE -ne 0) { throw "Audit tests failed." }

$pio = Join-Path $env:USERPROFILE ".platformio\penv\Scripts\platformio.exe"
if (-not (Test-Path -LiteralPath $pio)) { throw "PlatformIO launcher not found; use the PlatformIO terminal's pio command instead." }
foreach ($hand in @("left", "right")) {
  & $pio run -d firmware/esp32 -e "firebeetle32-$hand-glove-flex3-g500"
  if ($LASTEXITCODE -ne 0) { throw "Firmware build failed for $hand; stop before flashing." }
}
```

On Windows without g++, the host-C++ test is skipped. The two PlatformIO builds
above are still required; they compile the actual ESP32 firmware for both hands.

## 2. Flash one glove at a time

Close the recorder and serial monitors. Connect only the RIGHT ESP32 to USB.
Confirm it is physically the RIGHT glove. PlatformIO should select that board's
USB serial port; if several USB development boards are connected, disconnect
the others or specify the confirmed `--upload-port COM<number>`.

```powershell
& $pio run -d firmware/esp32 -e firebeetle32-right-glove-flex3-g500 -t upload
if ($LASTEXITCODE -ne 0) { throw "RIGHT upload failed." }
```

Keep the glove motionless on the table during automatic reboot/calibration,
at least 8 seconds. If you open a serial monitor to check the startup message,
it may reset the board; again keep it still. Do not capture/store pairing passkeys.

Disconnect RIGHT USB. Connect only LEFT, then:

```powershell
& $pio run -d firmware/esp32 -e firebeetle32-left-glove-flex3-g500 -t upload
if ($LASTEXITCODE -ne 0) { throw "LEFT upload failed." }
```

Keep LEFT still on the table for at least 8 seconds. Then power both gloves as
usual. A later power cycle also requires stillness during calibration.

## 3. Short motion capture, not full training collection

Start the flexible GUI:

```powershell
.\.venv\Scripts\python.exe -m src.hand_ai_flex3.record_both_gui
```

Select BOTH (or test one glove at a time), P01,
session `P01_gyro500_check_20261028`, split train, output root
`data/hand_flex3_checks/dual_pilot`, actions WASH, additional captures 3.
The train setting here is the recorder's existing metadata field; keep these
files in the diagnostic root and out of the final training dataset.

Move naturally as in gameplay, not deliberately slower to evade clipping.
Use the same glove fit and orientation planned for final recording.
RIGHT index remains faulty; this diagnostic checks the gyro, not the flex repair.

## 4. Audit and package

For BOTH:

```powershell
.\.venv\Scripts\python.exe -m tools.gyro_trial_audit `
  --session P01_gyro500_check_20261028 `
  --out .comms-local/gyro500/wash_check_v1.json
```

For RIGHT only add `--hands RIGHT`; for LEFT only add `--hands LEFT`.
The report is written even when status is blocked or review_headroom and exit
code is 2. Do not discard it. Existing report files are never overwritten;
choose a fresh output name for a rerun.

- `blocked`: invalid files, missing requested trials, or a gyro at an exact
  int16 wire endpoint. Fix the capture/transport problem before full collection.
- `review_headroom`: one or more gyro readings reach magnitude 300 deg/s.
  This is a conservative margin warning, not proof of clipping. Review the
  report before deciding whether the wire encoding needs a wider range.
- `headroom_observed`: the requested short captures are structurally sound
  and below the warning threshold. This is evidence about those captures only.

The checker validates CSV hashes, hand/session identities, source continuity,
uint32 wrap, timing, finite values and the v4 numerical grid. It does not verify
physical flex health, sensor register contents, sample generation before BLE,
gesture labels, final model accuracy or complete collection readiness.

Package the session into Downloads using the helper from the flexible-GUI kit:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\export_glove_session.ps1 -Session P01_gyro500_check_20261028
```

Send that ZIP and the short console audit summary. If WASH has headroom, test
the other intended gestures in this same diagnostic session before beginning
the full plan. For a later full diagnostic audit use:

```powershell
.\.venv\Scripts\python.exe -m tools.gyro_trial_audit `
  --session P01_gyro500_check_20261028 `
  --gestures PICKUP DROP CHOP WASH THROW POURING IDLE `
  --out .comms-local/gyro500/all_gestures_v1.json
```

Do not combine the earlier clipped ±250 WASH data with the corrected full
training set. Keep all raw diagnostic files/backups for provenance.

## Fallback and teammate impact

To restore the earlier sensor range, upload the original hand-specific
`firebeetle32-<left|right>-glove-flex3` environment, then recalibrate still.
Removing installed source files is not necessary to roll back the firmware.

Tell the comms teammate: opt-in IMU ±500 diagnostic environments were added;
physical units, v4 gyro scale 100, packet size, IDs and server format remain
unchanged. The sensor driver verifies its configured range. Production board
deployment, trained models and the FPGA pair were not changed. If natural
motion exceeds the wire limit, coordinate an explicitly versioned wider
encoding across firmware, recorder, bridge and server rather than changing
the meaning of existing v4 bytes.

After physical review, stage only the files in this package and the selected
small diagnostic report, run `git diff --cached --check`, then create a reviewed
commit/PR. Do not commit raw datasets or unrelated event-transport experiments.

## Primary references

- TDK MPU-6500 register map, section 4.6, register 27: GYRO_FS_SEL selects
  ±250/500/1000/2000 dps.
  https://invensense.tdk.com/wp-content/uploads/2015/02/MPU-6500-Register-Map2.pdf
- TDK MPU-6500 product specification, section 3.1: sensitivity 131/65.5/32.8/16.4
  LSB per deg/s respectively.
  https://s3.amazonaws.com/snapeda/datasheet/MPU-6500_TDK_InvenSense.pdf
- Reviewed baseline sources fetched from B07-CG4002/Let-Them-Cook main on
  2026-10-28; source hashes are recorded in the package installer manifest.
