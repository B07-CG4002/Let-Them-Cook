# Two-Glove Sensor Hardware

This directory contains the firmware and Python recorder for the CG4002 two-glove sensor system.

## Hardware overview

The system uses two instrumented gloves:

| Glove | Controller | IMU | Flex sensors | Serial port |
|---|---|---|---:|---|
| Left | ESP32 | 1 MPU-6500 | 2 | COM3 |
| Right | ESP32 | 1 MPU-6500 | 2 | COM4 |

Each glove produces 11 sensor values per sample:

```text
ax, ay, az, gx, gy, gz, yaw, pitch, roll, flex1, flex2
```

The two gloves therefore provide 22 features per synchronized sample.

The hardware photos, wiring references, and schematic should be kept with the project documentation. The exact physical connections should be checked against the schematic and the assembled glove before powering the circuit.

## ESP32 firmware

The firmware file is:

```text
imu_v3.ino
```

Upload the same firmware to the ESP32 on each glove using the Arduino IDE. Select the correct ESP32 board and COM port for each controller before uploading.

### Firmware settings

The firmware uses the following connections:

```text
MPU-6500 I2C address: 0x68
SDA: GPIO 21
SCL: GPIO 22
Flex sensor 1: GPIO 34
Flex sensor 2: GPIO 35
Serial baud rate: 115200
Output rate: approximately 50 Hz
```

The firmware performs gyroscope calibration after startup. Keep the glove and IMU completely still during calibration.

The IMU output includes:

- Gravity-compensated acceleration: `ax`, `ay`, `az`
- Bias-corrected gyroscope values: `gx`, `gy`, `gz`
- Orientation estimates: `yaw`, `pitch`, `roll`
- Normalized flex-sensor readings: `flex1`, `flex2`

## Python recorder

The recorder file is:

```text
imu_recording_terminal_v10.py
```

Install the serial dependency on the laptop if needed:

```powershell
pip install pyserial
```

Before running the recorder, close Arduino Serial Monitor and any other application using the COM ports.

The relevant port settings are:

```python
LEFT_PORT = "COM3"
RIGHT_PORT = "COM4"
BAUD = 115200
```

Run the recorder from this directory:

```powershell
python imu_recording_terminal_v10.py
```

The recorder connects to both gloves, displays the latest sensor values, and provides a timed recording interface.

## Recording procedure

1. Connect both ESP32 gloves by USB.
2. Confirm that the left glove is on `COM3` and the right glove is on `COM4`.
3. Upload the firmware to both gloves if required.
4. Keep both gloves still during startup calibration.
5. Start `imu_recording_terminal_v10.py`.
6. Confirm that the interface reports:

```text
Both gloves connected.
```

7. Enter the number of recordings and filenames.
8. During each six-second recording interval, perform the intended gesture.
9. Keep still during the countdown and rest intervals.
10. Check that both left- and right-hand CSV files were created.

Example filenames:

```text
PICKUPHOLD_LEFT_01_timestamped.csv
PICKUPHOLD_RIGHT_01_timestamped.csv
```

## Recording output

Recordings are stored using this structure:

```text
recordings/
├── left_hand/
│   └── <left_filename>/
│       └── <left_filename>_01_timestamped.csv
└── right_hand/
    └── <right_filename>/
        └── <right_filename>_01_timestamped.csv
```

Each CSV row contains:

```text
timestamp_utc,
elapsed_s,
sample_index,
ax_g, ay_g, az_g,
gx_deg_s, gy_deg_s, gz_deg_s,
yaw_deg, pitch_deg, roll_deg,
flex_1, flex_2
```

The left- and right-hand files in a pair use the same timestamp and sample index for each matched sample.

## Synchronization note

The two ESP32 controllers are not hardware-synchronized. The Python recorder reads both serial streams and pairs samples using their laptop reception timestamps. Samples are paired when their timestamps are within the configured alignment tolerance of 35 ms.

Therefore, the system provides software timestamp alignment rather than true simultaneous sampling.

## Sensor checks

Before recording labelled training data:

- Confirm both gloves continuously produce 11-value rows.
- Bend and straighten each flex sensor and confirm its value changes.
- Check that IMU values change when the glove moves.
- Confirm the left and right CSV files have matching sample indices.
- Check that the number of paired samples is reasonable for the recording duration.

Random movements may be used for hardware debugging, but should not be added as labelled gesture-training examples.

## Documentation evidence

Recommended evidence for the subsystem demonstration includes:

- Photos of both gloves laid out clearly.
- Photos of the left and right ESP32 connections.
- Photos showing the IMUs and flex sensors.
- The glove wiring schematic.
- A video showing flex-sensor response.
- A video showing both gloves connected and producing sensor readings.
- A screenshot of the recorder reporting that both gloves are connected.
- Example paired left/right CSV files.

## Current project status

- Both gloves connected and producing sensor readings.
- Left glove assigned to `COM3`.
- Right glove assigned to `COM4`.
- Two-glove recorder tested successfully.
- Paired CSV output generated successfully.
- The FPGA/PYNQ accelerator proof-of-concept is documented separately.
