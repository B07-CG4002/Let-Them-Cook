# ESP32 glove movement detector

This project records paired glove data, trains an IDLE/MOVING classifier, runs the classifier on the ESP32s, and lets you try different probability thresholds from the PC.

## Files

| File | What it does |
| --- | --- |
| `debug_model_sample_recording_v2.py` | Connects to both paired Bluetooth gloves and records labelled 5-second samples. Saves one paired CSV per sample under `recordings/idle/` or `recordings/moving/`. |
| `split_recordings_into_1s_v1.py` | Optionally splits paired recordings into one-second CSVs for each hand. |
| `train_movement_model_v2.py` | Trains a shared logistic-regression model, evaluates it, selects a movement threshold, and writes `movement_model_params.h` plus `movement_model_metrics.json`. |
| `debug_esp32_ml_movement_detector_v1.ino` | ESP32 sketch that reads the generated model header, calculates movement on each glove, and includes the state in its Bluetooth telemetry. |
| `debug_model_pc_threshold_test_v1.py` | Runs the model on the PC from the live Bluetooth sensor stream. Its slider lets you try thresholds without reflashing the gloves. |

## Install what you need

Use Python 3 on the PC. From PowerShell or a terminal, install the Python packages with:

```powershell
py -m pip install --upgrade pip
py -m pip install pyserial numpy scikit-learn
```

The recorder and PC tester use Tkinter for their windows. Tkinter is included with the standard Windows Python installer. If Python reports that `tkinter` is missing, rerun the Python installer and enable **Tcl/Tk and IDLE**.

For the ESP32 sketch, install the **ESP32 Arduino board package** in Arduino IDE and select the correct ESP32 board and port. `Wire` and `BluetoothSerial` come with the ESP32 Arduino core; no separate Arduino library install is needed for them. The sketch uses Bluetooth Classic, so the selected ESP32 board must support it.

## 1. Record labelled samples

Pair both gloves with Windows Bluetooth first. Start the recorder from the project folder:

```powershell
py .\debug_model_sample_recording_v2.py
```

In the window:

1. Enter a person ID, for example `P00`.
2. Choose `IDLE` or `MOVING` and enter the number of recordings.
3. Confirm that both left and right data windows are receiving packets and that their movement indicators show a state.
4. Select **Start recording**. Each sample follows a 3-second wait, then a 5-second recording; this repeats for the requested number of recordings. **Cancel** stops the run and discards the sample currently being recorded.

The script creates the output folders as needed. Example paired files:

```text
recordings/idle/P00_IDLE_1.csv
recordings/moving/P00_MOVING_1.csv
```

Record both classes across the people, sessions, and situations you expect the gloves to encounter. Keep the classes reasonably balanced. Each CSV contains both hands' sensor columns; the IDLE/MOVING field shown from the firmware is not added as a sensor column because the recorder's selected class is the label.

## 2. Optionally split recordings into one-second files

The trainer can use the paired 5-second CSV files directly, so splitting is optional. To also create per-hand one-second CSVs, run:

```powershell
py .\split_recordings_into_1s_v1.py .\recordings\idle .\recordings\moving
```

This puts `left` and `right` folders under each class folder, for example:

```text
recordings/idle/left/P00_IDLE_1_LEFT_1s_01.csv
recordings/idle/right/P00_IDLE_1_RIGHT_1s_01.csv
recordings/moving/left/P00_MOVING_1_LEFT_1s_01.csv
recordings/moving/right/P00_MOVING_1_RIGHT_1s_01.csv
```

The trainer accepts either the paired recordings or these splits. If both are present, it recognizes the split files from each paired recording and avoids counting the same source recording twice.

You can rerun the split command after recording more samples; you do not need to delete the existing left and right folders first. The splitter reads only CSV files directly inside recordings/idle and recordings/moving, so it skips the one-second files in those subfolders. It overwrites the split files for each existing master recording and creates splits for new master recordings. If you delete or rename a master CSV, its old split files are not automatically removed.

## 3. Train the model

Run from the project folder. For example, set the output directory to the folder that contains the ESP32 `.ino` file:

```powershell
py .\train_movement_model_v2.py .\recordings\idle .\recordings\moving --output-dir .\debug_esp32_ml_movement_detector_v1 --max-idle-false-moving-rate 0.30
```

Change `0.30` to the maximum IDLE validation-window false-MOVING rate you want to try. The script uses recordings grouped by source trial for its fit, threshold-selection, and held-out test splits. It chooses a threshold that maximizes MOVING recall while meeting that validation limit, then reports held-out metrics. Review `movement_model_metrics.json` after training.

The command writes these files into the selected output folder:

```text
debug_esp32_ml_movement_detector_v1/movement_model_params.h
debug_esp32_ml_movement_detector_v1/movement_model_metrics.json
```

The header contains the learned weights, scaling values, and selected threshold. Training starts from the current recordings each time; it does not continue training from an older header.

## 4. Run the model on the ESP32 gloves

Put `debug_esp32_ml_movement_detector_v1.ino` and the generated `movement_model_params.h` together in an Arduino sketch folder named `debug_esp32_ml_movement_detector_v1` (the folder name should match the `.ino` filename). If you trained into that folder, the header is already there.

The sketch currently has the RIGHT glove name and ID. Before uploading to the LEFT glove, change these two constants near the top of the sketch:

```cpp
const char *BLUETOOTH_DEVICE_NAME = "SENSOR_GLOVE_LEFT";
const char *GLOVE_ID = "SENS_GL_LEFT";
```

Upload the sketch once to the left ESP32. Then set those two constants back to `SENSOR_GLOVE_RIGHT` and `SENS_GL_RIGHT`, select the right ESP32's port, and upload once to the right ESP32. Both builds should use the same generated model header. Select the correct COM port for each upload.

Each glove streams its sensor values, hand ID, and its current firmware IDLE/MOVING state over Bluetooth. The firmware needs a full one-second window of sensor packets before it can first classify movement, and uses consecutive windows to reduce state flicker.

## 5. Try thresholds on the PC

With both gloves powered on and paired, run:

```powershell
py .\debug_model_pc_threshold_test_v1.py --model-header .\debug_esp32_ml_movement_detector_v1\movement_model_params.h
```

The window shows each glove's sensor stream, the model's MOVING probability and PC-classified state, and the state sent by the ESP32. Adjust the slider to compare thresholds live; this changes only the PC result and does not reflash or modify either glove. The ESP32 continues using the threshold compiled into its header until you retrain and upload the sketch again.

## Typical run order

```text
Record IDLE and MOVING samples
        ↓
Optionally split samples into one-second, per-hand CSVs
        ↓
Train; inspect movement_model_metrics.json
        ↓
Try thresholds live with the PC tester
        ↓
Upload the sketch and generated header to each ESP32
```
