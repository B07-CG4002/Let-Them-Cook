from pathlib import Path
from collections import deque
import json
import time

import numpy as np
import serial
import torch

from src.model import GestureCNN


WINDOW_SIZE = 50
NUM_FEATURES_PER_GLOVE = 11
NUM_FEATURES = 22

LEFT_PORT = "COM3"
RIGHT_PORT = "COM4"
BAUD_RATE = 115200
SERIAL_TIMEOUT = 2

MODEL_PATH = Path("models/gesture_cnn.pth")
CLASS_NAMES_PATH = Path("models/class_names.json")


def load_model():
    with open(CLASS_NAMES_PATH, "r", encoding="utf-8") as file:
        class_names = json.load(file)

    model = GestureCNN(
        num_features=NUM_FEATURES,
        num_classes=len(class_names),
    )

    model.load_state_dict(torch.load(MODEL_PATH, map_location="cpu"))
    model.eval()

    return model, class_names


def parse_sensor_line(line):
    """
    Parses either of these formats:

    Labelled:
    ax=... | ay=... | ... | flex1=... | flex2=...

    CSV:
    0.0347,0.0168,-0.0105,2.9535,-1.6109,5.2350,
    107.81,-24.25,152.78,1.0000,1.0000
    """

    line = line.strip()

    if not line:
        raise ValueError("Empty sensor line")

    # CSV format from the gloves
    if "," in line and "=" not in line:
        values = [
            float(value.strip())
            for value in line.split(",")
        ]

    # Labelled format
    else:
        values = []

        for field in line.split("|"):
            field = field.strip()

            if "=" not in field:
                continue

            _, value = field.split("=", 1)
            values.append(float(value.strip()))

    if len(values) != NUM_FEATURES_PER_GLOVE:
        raise ValueError(
            f"Expected {NUM_FEATURES_PER_GLOVE} values, "
            f"received {len(values)} from line: {line}"
        )

    return values


def read_glove_sample(left_serial, right_serial):
    left_line = left_serial.readline().decode(
        "utf-8", errors="ignore"
    ).strip()

    right_line = right_serial.readline().decode(
        "utf-8", errors="ignore"
    ).strip()

    if not left_line or not right_line:
        raise ValueError("No data received from one or both gloves")

    left_values = parse_sensor_line(left_line)
    right_values = parse_sensor_line(right_line)

    # Combined sample:
    # left glove = 11 features
    # right glove = 11 features
    # total = 22 features
    return np.asarray(left_values + right_values, dtype=np.float32)


def predict(model, class_names, window):
    """
    Model input shape:
        batch, features, time
    """

    input_data = np.asarray(window, dtype=np.float32)

    if input_data.shape != (WINDOW_SIZE, NUM_FEATURES):
        raise ValueError(
            f"Expected window shape "
            f"({WINDOW_SIZE}, {NUM_FEATURES}), "
            f"received {input_data.shape}"
        )

    input_tensor = torch.tensor(
        input_data,
        dtype=torch.float32,
    ).transpose(0, 1).unsqueeze(0)

    with torch.no_grad():
        output = model(input_tensor)
        probabilities = torch.softmax(output, dim=1)
        confidence, prediction = torch.max(probabilities, dim=1)

    return (
        class_names[prediction.item()],
        confidence.item(),
    )


def print_prediction(gesture, confidence):
    status = "VALID" if confidence >= 0.70 else "LOW_CONFIDENCE"

    print(
        {
            "gesture": gesture,
            "confidence": round(confidence, 4),
            "status": status,
        }
    )


def main():
    model, class_names = load_model()
    window = deque(maxlen=WINDOW_SIZE)

    left_serial = None
    right_serial = None

    try:
        print(f"Connecting to left glove on {LEFT_PORT}...")
        left_serial = serial.Serial(
            LEFT_PORT,
            BAUD_RATE,
            timeout=SERIAL_TIMEOUT,
        )

        print(f"Connecting to right glove on {RIGHT_PORT}...")
        right_serial = serial.Serial(
            RIGHT_PORT,
            BAUD_RATE,
            timeout=SERIAL_TIMEOUT,
        )

        # Give both ESP32 boards time to reset after opening the ports.
        time.sleep(2)

        # Clear any incomplete startup lines.
        left_serial.reset_input_buffer()
        right_serial.reset_input_buffer()

        print("Both gloves connected.")
        print("Collecting samples...")
        print(
            f"Waiting for {WINDOW_SIZE} samples "
            f"with {NUM_FEATURES} features each..."
        )
        print("Press Ctrl+C to stop.")

        while True:
            try:
                sample = read_glove_sample(
                    left_serial,
                    right_serial,
                )

                window.append(sample)

                if len(window) < WINDOW_SIZE:
                    print(
                        f"\rSamples collected: "
                        f"{len(window)}/{WINDOW_SIZE}",
                        end="",
                        flush=True,
                    )
                    continue

                gesture, confidence = predict(
                    model,
                    class_names,
                    window,
                )

                print()
                print_prediction(gesture, confidence)

            except ValueError as error:
                print(f"\nSkipping invalid sample: {error}")

    except serial.SerialException as error:
        print(f"\nSerial connection error: {error}")
        print(
            "Check that both gloves are connected and that "
            "COM3/COM4 are the correct ports."
        )

    except KeyboardInterrupt:
        print("\nStopping live inference...")

    finally:
        if left_serial is not None and left_serial.is_open:
            left_serial.close()

        if right_serial is not None and right_serial.is_open:
            right_serial.close()

        print("Serial connections closed.")


if __name__ == "__main__":
    main()