"""Run the glove movement model on the PC and adjust its threshold live.

The ESP32 sends 11 sensor values plus a glove ID (and optionally its own
IDLE/MOVING state). This script ignores the ESP32 state and runs the trained
model locally using movement_model_params.h. Requires pyserial and numpy.

Example:
    python debug_model_pc_threshold_test_v1.py \
        --model-header esp32_ml_movement_detector_v1/movement_model_params.h
"""
import argparse
from collections import deque
import math
from pathlib import Path
import re
import threading
import time
import tkinter as tk

import numpy as np
import serial
from serial.tools import list_ports


BAUD = 115200
PORT_IDENTIFICATION_TIMEOUT_SECONDS = 2.0
PORT_RETRY_DELAY_SECONDS = 0.5
MODEL_WINDOW_SAMPLES = 50          # 1 second at 50 Hz
MODEL_UPDATE_INTERVAL_SECONDS = 0.2  # Evaluate 5 times per second
MODEL_MOVING_CONFIRM_WINDOWS = 2
MODEL_IDLE_CONFIRM_WINDOWS = 3
SENSOR_FIELD_COUNT = 11
DEVICE_LABEL_TO_HAND = {"SENS_GL_LEFT": "LEFT", "SENS_GL_RIGHT": "RIGHT"}
FEATURE_NAMES = (
    "accel_std_x", "accel_std_y", "accel_std_z",
    "gyro_rms_x", "gyro_rms_y", "gyro_rms_z",
    "flex_range_1", "flex_range_2",
)
SENSOR_NAMES = (
    "ax", "ay", "az", "gx", "gy", "gz",
    "yaw", "pitch", "roll", "flex1", "flex2",
)


def load_model_header(path):
    """Read the generated model constants from movement_model_params.h."""
    text = Path(path).read_text(encoding="utf-8")
    trained = re.search(r"^\s*#define\s+MOVEMENT_MODEL_TRAINED\s+(\d+)", text, re.MULTILINE)
    if not trained or trained.group(1) != "1":
        raise ValueError("The header does not contain a trained model. Train the model first.")

    def read_array(name):
        match = re.search(
            rf"static\s+const\s+float\s+{re.escape(name)}\s*\[[^]]+\]\s*=\s*\{{([^}}]*)\}}\s*;",
            text,
        )
        if not match:
            raise ValueError(f"Could not find {name} in the model header")
        try:
            values = np.asarray([
                float(token.strip().rstrip("fF"))
                for token in match.group(1).split(",") if token.strip()
            ], dtype=np.float64)
        except ValueError as exc:
            raise ValueError(f"Invalid number in {name}") from exc
        if values.size != len(FEATURE_NAMES) or not np.all(np.isfinite(values)):
            raise ValueError(f"{name} must contain {len(FEATURE_NAMES)} finite values")
        return values

    def read_scalar(name):
        match = re.search(
            rf"static\s+const\s+float\s+{re.escape(name)}\s*=\s*([+-]?[\d.eE+-]+)f\s*;",
            text,
        )
        if not match:
            raise ValueError(f"Could not find {name} in the model header")
        value = float(match.group(1))
        if not math.isfinite(value):
            raise ValueError(f"{name} is not finite")
        return value

    mean = read_array("MOVEMENT_FEATURE_MEAN")
    scale = read_array("MOVEMENT_FEATURE_SCALE")
    coefficients = read_array("MOVEMENT_LOGISTIC_COEFFICIENTS")
    scale[scale == 0.0] = 1.0
    intercept = read_scalar("MOVEMENT_LOGISTIC_INTERCEPT")
    threshold = read_scalar("MOVEMENT_PROBABILITY_THRESHOLD")
    return {
        "mean": mean,
        "scale": scale,
        "coefficients": coefficients,
        "intercept": intercept,
        "threshold": min(1.0, max(0.0, threshold)),
    }


def parse_packet(raw):
    """Return hand, 11 sensor values, and optional ESP32 state from a packet."""
    try:
        fields = raw.decode("ascii").strip().split(",")
        if len(fields) == 13:
            hand_label, firmware_state = fields[-2].upper(), fields[-1].upper()
            if firmware_state not in ("IDLE", "MOVING"):
                return None
            sensor_fields = fields[:SENSOR_FIELD_COUNT]
        elif len(fields) == 12:  # Also accept firmware packets without a state field.
            hand_label, firmware_state = fields[-1].upper(), "NOT SENT"
            sensor_fields = fields[:SENSOR_FIELD_COUNT]
        else:
            return None
        hand = DEVICE_LABEL_TO_HAND.get(hand_label)
        if hand is None:
            return None
        values = tuple(float(value) for value in sensor_fields)
        if not all(math.isfinite(value) for value in values):
            return None
        return hand, values, firmware_state
    except (UnicodeDecodeError, ValueError):
        return None


def calculate_features(samples):
    """Match the trainer and ESP32 1-second feature calculations."""
    data = np.asarray(samples, dtype=np.float64)
    if data.shape != (MODEL_WINDOW_SAMPLES, SENSOR_FIELD_COUNT):
        raise ValueError("Expected a full one-second window of 50 sensor packets")
    accel_std = np.std(data[:, 0:3], axis=0, ddof=0)
    gyro_rms = np.sqrt(np.mean(np.square(data[:, 3:6]), axis=0))
    flex_range = np.ptp(data[:, 9:11], axis=0)
    return np.concatenate((accel_std, gyro_rms, flex_range))


def predict_probability(features, model):
    standardized = (features - model["mean"]) / model["scale"]
    score = model["intercept"] + float(np.dot(model["coefficients"], standardized))
    if score >= 0.0:
        exp_term = math.exp(-min(score, 709.0))
        return 1.0 / (1.0 + exp_term)
    exp_term = math.exp(max(score, -709.0))
    return exp_term / (1.0 + exp_term)


def bluetooth_port_candidates():
    candidates = []
    for port in list_ports.comports():
        details = f"{port.description} {port.hwid}".lower()
        if "bluetooth" in details or "bthenum" in details:
            candidates.append(port.device)
    return candidates


def identify_glove(port, stop_event):
    ser = None
    pending = bytearray()
    try:
        ser = serial.Serial(port, BAUD, timeout=0.05)
        deadline = time.perf_counter() + PORT_IDENTIFICATION_TIMEOUT_SECONDS
        while time.perf_counter() < deadline and not stop_event.is_set():
            chunk = ser.read(min(max(ser.in_waiting, 1), 4096))
            if not chunk:
                continue
            pending.extend(chunk)
            while b"\n" in pending:
                raw, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                packet = parse_packet(raw)
                if packet is not None:
                    return packet[0], ser
            if len(pending) > 65536:
                pending.clear()
    except (serial.SerialException, OSError):
        pass
    if ser is not None:
        try:
            ser.close()
        except (serial.SerialException, OSError):
            pass
    return None


def connect_until_both_ready(stop_event):
    while not stop_event.is_set():
        found = {}
        try:
            for port in bluetooth_port_candidates():
                if stop_event.is_set():
                    return None
                identified = identify_glove(port, stop_event)
                if identified is None:
                    continue
                hand, ser = identified
                if hand in found:
                    ser.close()
                    continue
                found[hand] = (port, ser)
                if "LEFT" in found and "RIGHT" in found:
                    result = (
                        found["LEFT"][0], found["LEFT"][1],
                        found["RIGHT"][0], found["RIGHT"][1],
                    )
                    found = {}
                    return result
        finally:
            for _, ser in found.values():
                try:
                    ser.close()
                except (serial.SerialException, OSError):
                    pass
        if stop_event.wait(PORT_RETRY_DELAY_SECONDS):
            return None
    return None


def update_model_state(hand_data, model, threshold, now):
    """Append a packet and update the debounced PC-side output when due."""
    hand_data["samples"].append(hand_data["latest_values"])
    if len(hand_data["samples"]) < MODEL_WINDOW_SAMPLES:
        hand_data["pc_state"] = "WARMING UP"
        return
    if now - hand_data["last_inference"] < MODEL_UPDATE_INTERVAL_SECONDS:
        return
    hand_data["last_inference"] = now
    features = calculate_features(list(hand_data["samples"]))
    probability = predict_probability(features, model)
    hand_data["probability"] = probability

    if probability >= threshold:
        hand_data["moving_votes"] += 1
        hand_data["idle_votes"] = 0
        if hand_data["pc_state"] != "MOVING" and hand_data["moving_votes"] >= MODEL_MOVING_CONFIRM_WINDOWS:
            hand_data["pc_state"] = "MOVING"
    else:
        hand_data["idle_votes"] += 1
        hand_data["moving_votes"] = 0
        if hand_data["pc_state"] != "IDLE" and hand_data["idle_votes"] >= MODEL_IDLE_CONFIRM_WINDOWS:
            hand_data["pc_state"] = "IDLE"


def serial_worker(hand, port, ser, shared, lock, stop_event, errors, model):
    pending = bytearray()
    key = hand.lower()
    try:
        while not stop_event.is_set():
            chunk = ser.read(min(max(ser.in_waiting, 1), 4096))
            if not chunk:
                continue
            pending.extend(chunk)
            while b"\n" in pending:
                raw, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                packet = parse_packet(raw)
                if packet is None or packet[0] != hand:
                    continue
                _, values, firmware_state = packet
                now = time.perf_counter()
                with lock:
                    hand_data = shared["hands"][key]
                    hand_data["latest_values"] = values
                    hand_data["firmware_state"] = firmware_state
                    hand_data["latest_packet_time"] = now
                    hand_data["latest_line"] = " | ".join(
                        f"{name}={value:.3f}" for name, value in zip(SENSOR_NAMES, values)
                    )
                    update_model_state(hand_data, model, shared["threshold"], now)
            if len(pending) > 65536:
                pending.clear()
    except (serial.SerialException, OSError) as exc:
        with lock:
            errors.append(f"{port}: {exc}")
        stop_event.set()
    finally:
        try:
            ser.close()
        except (serial.SerialException, OSError):
            pass


def make_hand_data():
    return {
        "samples": deque(maxlen=MODEL_WINDOW_SAMPLES),
        "latest_values": None,
        "latest_line": "Waiting for sensor packets...",
        "latest_packet_time": None,
        "firmware_state": "NOT SENT",
        "pc_state": "WARMING UP",
        "probability": None,
        "moving_votes": 0,
        "idle_votes": 0,
        "last_inference": 0.0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-header", type=Path, default=Path("movement_model_params.h"),
                        help="trained movement_model_params.h file (default: ./movement_model_params.h)")
    args = parser.parse_args()
    try:
        model = load_model_header(args.model_header)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    stop_event = threading.Event()
    lock = threading.Lock()
    shared = {
        "threshold": model["threshold"],
        "hands": {"left": make_hand_data(), "right": make_hand_data()},
        "ports": {"left": None, "right": None},
        "errors": [],
        "cycle_stop": None,
    }

    root = tk.Tk()
    root.title("PC Movement Model Threshold Tester")
    root.geometry("1180x690")
    root.minsize(850, 560)

    connection_var = tk.StringVar(value="Window is open — searching for both Bluetooth gloves...")
    threshold_var = tk.DoubleVar(value=model["threshold"])
    threshold_text_var = tk.StringVar(value=f"PC threshold: {model['threshold']:.2f}")
    tk.Label(root, text="PC Movement Model Threshold Tester",
             font=("Segoe UI", 22, "bold")).pack(pady=(14, 4))
    tk.Label(root, textvariable=connection_var, font=("Segoe UI", 11)).pack(pady=(0, 8))

    threshold_frame = tk.Frame(root, padx=10, pady=4)
    threshold_frame.pack(fill="x")
    tk.Label(threshold_frame, textvariable=threshold_text_var,
             font=("Segoe UI", 14, "bold")).pack()
    tk.Scale(
        threshold_frame, from_=0.0, to=1.0, resolution=0.01, orient="horizontal",
        variable=threshold_var, length=650, showvalue=True,
    ).pack(fill="x", padx=40)
    tk.Label(threshold_frame,
             text="Changing this slider changes only the PC model; it does not change the ESP32 threshold.",
             font=("Segoe UI", 9)).pack(pady=(0, 6))

    panels = {}
    for key, title in (("left", "LEFT GLOVE"), ("right", "RIGHT GLOVE")):
        frame = tk.LabelFrame(root, text=title, padx=12, pady=8, font=("Segoe UI", 11, "bold"))
        frame.pack(fill="both", expand=True, padx=16, pady=7)
        state_label = tk.Label(frame, text="WARMING UP", width=18,
                               font=("Segoe UI", 22, "bold"), relief="ridge", padx=8, pady=5,
                               bg="#d9d9d9", fg="#222222")
        state_label.pack(pady=(2, 6))
        probability_var = tk.StringVar(value="P(MOVING): --")
        tk.Label(frame, textvariable=probability_var, font=("Segoe UI", 13)).pack()
        firmware_var = tk.StringVar(value="ESP32 packet state: NOT SENT")
        tk.Label(frame, textvariable=firmware_var, font=("Segoe UI", 10)).pack(pady=(1, 5))
        data_var = tk.StringVar(value="Waiting for sensor packets...")
        tk.Label(frame, textvariable=data_var, font=("Consolas", 9), wraplength=1080,
                 justify="left").pack(fill="x", pady=(2, 0))
        panels[key] = (state_label, probability_var, firmware_var, data_var)

    def on_threshold_change(value):
        threshold = float(value)
        threshold_text_var.set(f"PC threshold: {threshold:.2f}")
        with lock:
            shared["threshold"] = threshold

    threshold_var.trace_add("write", lambda *_: on_threshold_change(threshold_var.get()))

    def update_display():
        now = time.perf_counter()
        with lock:
            snapshot = {
                key: {
                    "pc_state": data["pc_state"],
                    "probability": data["probability"],
                    "firmware_state": data["firmware_state"],
                    "latest_line": data["latest_line"],
                    "latest_packet_time": data["latest_packet_time"],
                }
                for key, data in shared["hands"].items()
            }
            ports = dict(shared["ports"])
            errors = list(shared["errors"])

        if errors:
            connection_var.set("Connection error — retrying: " + " | ".join(errors[-2:]))
        elif ports["left"] and ports["right"]:
            connection_var.set(f"Connected — LEFT {ports['left']} | RIGHT {ports['right']}")
        else:
            connection_var.set("Searching for both Bluetooth gloves; retrying automatically...")

        for key, (state_label, probability_var, firmware_var, data_var) in panels.items():
            data = snapshot[key]
            state = data["pc_state"]
            if state == "MOVING":
                state_label.configure(text="MOVING", bg="#d9534f", fg="white")
            elif state == "IDLE":
                state_label.configure(text="IDLE", bg="#5cb85c", fg="white")
            elif data["latest_packet_time"] is not None and now - data["latest_packet_time"] > 1.0:
                state_label.configure(text="NO DATA", bg="#f0ad4e", fg="#222222")
            else:
                state_label.configure(text="WARMING UP", bg="#d9d9d9", fg="#222222")
            probability = data["probability"]
            probability_var.set("P(MOVING): --" if probability is None
                                else f"P(MOVING): {probability:.3f}")
            firmware_var.set(f"ESP32 packet state: {data['firmware_state']}")
            data_var.set(data["latest_line"])

        if not stop_event.is_set():
            root.after(50, update_display)

    def connection_manager():
        while not stop_event.is_set():
            result = connect_until_both_ready(stop_event)
            if result is None:
                return
            left_port, left_serial, right_port, right_serial = result
            with lock:
                shared["ports"]["left"] = left_port
                shared["ports"]["right"] = right_port
                shared["errors"].clear()
                for hand in ("left", "right"):
                    shared["hands"][hand].update(make_hand_data())
            cycle_stop = threading.Event()
            with lock:
                shared["cycle_stop"] = cycle_stop
            workers = [
                threading.Thread(target=serial_worker,
                                 args=("LEFT", left_port, left_serial, shared, lock,
                                       cycle_stop, shared["errors"], model), daemon=True),
                threading.Thread(target=serial_worker,
                                 args=("RIGHT", right_port, right_serial, shared, lock,
                                       cycle_stop, shared["errors"], model), daemon=True),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            with lock:
                shared["ports"]["left"] = None
                shared["ports"]["right"] = None
                shared["cycle_stop"] = None
                for hand in ("left", "right"):
                    shared["hands"][hand]["pc_state"] = "WARMING UP"
                    shared["hands"][hand]["probability"] = None
            if not stop_event.is_set():
                with lock:
                    shared["errors"].append("Connection lost — retrying both gloves.")

    def close():
        stop_event.set()
        with lock:
            cycle_stop = shared["cycle_stop"]
        if cycle_stop is not None:
            cycle_stop.set()
        root.destroy()

    threading.Thread(target=connection_manager, daemon=True).start()
    root.after(50, update_display)
    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()
    stop_event.set()


if __name__ == "__main__":
    main()
