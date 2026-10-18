"""Record paired left/right IMU windows labelled IDLE or MOVING.

The ESP32 gloves must emit the comma-separated 11 sensor values followed by
SENS_GL_LEFT or SENS_GL_RIGHT and an IDLE or MOVING model state. The current
model state is shown in a separate indicator for each hand and is not added to
the saved CSV files. Pair both gloves over Bluetooth before starting. Requires
pyserial.
"""
import csv
from collections import deque
from datetime import datetime, timezone
import math
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import messagebox

import serial
from serial.tools import list_ports


BAUD = 115200
PORT_IDENTIFICATION_TIMEOUT_SECONDS = 2.0
PORT_RETRY_DELAY_SECONDS = 0.5
MAX_SAMPLE_ALIGNMENT_GAP_SECONDS = 0.035
WAIT_SECONDS = 3.0
RECORD_SECONDS = 5.0
RECORDING_DIRS = {
    "IDLE": Path("recordings") / "idle",
    "MOVING": Path("recordings") / "moving",
}
GESTURES = ("IDLE", "MOVING")
SENSOR_NAMES = [
    "ax_g", "ay_g", "az_g", "gx_deg_s", "gy_deg_s", "gz_deg_s",
    "yaw_deg", "pitch_deg", "roll_deg", "flex_1", "flex_2",
]
FIELD_NAMES = (
    "ax", "ay", "az", "gx", "gy", "gz",
    "yaw", "pitch", "roll", "flex1", "flex2",
)
DEVICE_LABEL_TO_HAND = {"SENS_GL_LEFT": "LEFT", "SENS_GL_RIGHT": "RIGHT"}
CSV_HEADER = (
    ["timestamp_utc", "elapsed_s", "sample_index"]
    + [f"left_{name}" for name in SENSOR_NAMES]
    + [f"right_{name}" for name in SENSOR_NAMES]
)


def parse_sample(raw):
    """Return (LEFT/RIGHT, 11 sensor values, model state)."""
    try:
        fields = raw.decode("ascii").strip().split(",")
        if len(fields) != 13:
            return None
        hand = DEVICE_LABEL_TO_HAND.get(fields[-2].upper())
        if hand is None:
            return None
        if fields[-1].upper() not in ("IDLE", "MOVING"):
            return None
        values = tuple(float(value) for value in fields[:-2])
        if not all(math.isfinite(value) for value in values):
            return None
        return hand, values, fields[-1].upper()
    except (UnicodeDecodeError, ValueError):
        return None


def format_sample(values):
    return " | ".join(f"{name}={value: .4f}" for name, value in zip(FIELD_NAMES, values))


def open_bluetooth_port(port):
    """Open an already-paired Bluetooth SPP port without resetting the glove."""
    return serial.Serial(port, BAUD, timeout=0.05)


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
        ser = open_bluetooth_port(port)
        deadline = time.perf_counter() + PORT_IDENTIFICATION_TIMEOUT_SECONDS
        while time.perf_counter() < deadline and not stop_event.is_set():
            chunk = ser.read(min(max(ser.in_waiting, 1), 4096))
            if not chunk:
                continue
            pending.extend(chunk)
            while b"\n" in pending:
                raw, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                packet = parse_sample(raw)
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
                    result = (found["LEFT"][0], found["LEFT"][1],
                              found["RIGHT"][0], found["RIGHT"][1])
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


def serial_worker(hand, port, ser, latest_rows, samples, model_states, lock, stop_event, errors):
    pending = bytearray()
    try:
        while not stop_event.is_set():
            chunk = ser.read(min(max(ser.in_waiting, 1), 4096))
            if not chunk:
                continue
            pending.extend(chunk)
            while b"\n" in pending:
                raw, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                packet = parse_sample(raw)
                if packet is None or packet[0] != hand:
                    continue
                values, movement_state = packet[1], packet[2]
                received = time.perf_counter()
                timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                with lock:
                    latest_rows.append(format_sample(values))
                    model_states[hand.lower()] = movement_state
                    samples.append((received, timestamp, values))
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


def align_samples(left_samples, right_samples, start_time, end_time):
    """Pair by host receive time, keeping rows within 35 ms of one another."""
    left = [s for s in left_samples if start_time <= s[0] <= end_time]
    right = [s for s in right_samples if start_time <= s[0] <= end_time]
    pairs = []
    li = ri = 0
    while li < len(left) and ri < len(right):
        left_received, timestamp, left_values = left[li]
        right_received, _, right_values = right[ri]
        difference = left_received - right_received
        if abs(difference) <= MAX_SAMPLE_ALIGNMENT_GAP_SECONDS:
            pairs.append((left_received, timestamp, left_values, right_values))
            li += 1
            ri += 1
        elif difference < 0:
            li += 1
        else:
            ri += 1
    return pairs


def save_aligned_samples(path, left_samples, right_samples, start_time, end_time):
    pairs = align_samples(left_samples, right_samples, start_time, end_time)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for sample_index, (received, timestamp, left_values, right_values) in enumerate(pairs, 1):
            writer.writerow([
                timestamp,
                f"{received - start_time:.6f}",
                sample_index,
                *left_values,
                *right_values,
            ])
    return len(pairs)


def next_sample_ids(person, gesture, count):
    directory = RECORDING_DIRS[gesture]
    directory.mkdir(parents=True, exist_ok=True)
    prefix = f"{person}_{gesture}_"
    used = set()
    for path in directory.glob(f"{prefix}*.csv"):
        suffix = path.stem[len(prefix):]
        if suffix.isdigit():
            used.add(int(suffix))
    planned = []
    number = 1
    while len(planned) < count:
        if number not in used:
            planned.append(number)
        number += 1
    return planned


def main():
    stop_event = threading.Event()
    lock = threading.Lock()
    left_rows = deque(maxlen=5)
    right_rows = deque(maxlen=5)
    left_samples = deque(maxlen=10000)
    right_samples = deque(maxlen=10000)
    model_states = {"left": "WAITING", "right": "WAITING"}
    errors = []
    connection_lock = threading.Lock()
    active_serials = {"left": None, "right": None}

    root = tk.Tk()
    root.title("Two-Glove Movement Sample Recorder")
    root.geometry("1500x760")
    root.minsize(950, 600)

    phase_var = tk.StringVar(value="READY")
    countdown_var = tk.StringVar(value="Press Start Recording")
    status_var = tk.StringVar(value="Opening window — connecting to both gloves...")
    person_var = tk.StringVar(value="P02")
    gesture_var = tk.StringVar(value="IDLE")
    count_var = tk.StringVar(value="1")

    tk.Label(root, textvariable=phase_var, font=("Segoe UI", 28, "bold")).pack(pady=(16, 0))
    tk.Label(root, textvariable=countdown_var, font=("Segoe UI", 24)).pack(pady=(0, 8))
    tk.Label(root, textvariable=status_var, font=("Segoe UI", 11)).pack(pady=(0, 12))

    settings = tk.Frame(root)
    settings.pack(pady=(0, 12))
    tk.Label(settings, text="Person ID (for example P02):").grid(row=0, column=0, padx=5, pady=3, sticky="e")
    person_entry = tk.Entry(settings, textvariable=person_var, width=12)
    person_entry.grid(row=0, column=1, padx=5, pady=3, sticky="w")
    tk.Label(settings, text="Gesture:").grid(row=1, column=0, padx=5, pady=3, sticky="e")
    gesture_menu = tk.OptionMenu(settings, gesture_var, *GESTURES)
    gesture_menu.grid(row=1, column=1, padx=5, pady=3, sticky="w")
    tk.Label(settings, text="Number of recordings:").grid(row=2, column=0, padx=5, pady=3, sticky="e")
    count_entry = tk.Entry(settings, textvariable=count_var, width=12)
    count_entry.grid(row=2, column=1, padx=5, pady=3, sticky="w")
    filename_preview_var = tk.StringVar()
    tk.Label(settings, textvariable=filename_preview_var, justify="left").grid(
        row=3, column=0, columnspan=2, padx=5, pady=3, sticky="w")

    def update_filename_preview(*_):
        person = person_var.get().strip().upper() or "P02"
        gesture = gesture_var.get().strip().upper() or "IDLE"
        folder = RECORDING_DIRS.get(gesture, RECORDING_DIRS["IDLE"]).as_posix()
        filename_preview_var.set(f"Saving to ./{folder}/\n{person}_{gesture}_<sample id>.csv")

    person_var.trace_add("write", update_filename_preview)
    gesture_var.trace_add("write", update_filename_preview)
    update_filename_preview()

    panels = []
    movement_indicators = []
    for hand, title in (("left", "Left Hand — Bluetooth"),
                        ("right", "Right Hand — Bluetooth")):
        frame = tk.Frame(root, padx=8, pady=5)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=title, font=("Segoe UI", 11, "bold")).pack(pady=(0, 3))
        indicator = tk.Label(
            frame,
            text="WAITING",
            width=18,
            font=("Segoe UI", 18, "bold"),
            relief="ridge",
            padx=8,
            pady=4,
            bg="#d9d9d9",
            fg="#222222",
        )
        indicator.pack(pady=(0, 5))
        movement_indicators.append(indicator)
        display = tk.Text(frame, wrap="none", height=5, state="disabled",
                          font=("Consolas", 10), padx=8, pady=6)
        display.pack(fill="both", expand=True)
        panels.append(display)

    controls = tk.Frame(root)
    controls.pack(pady=12)
    start_button = tk.Button(controls, text="Start Recording", width=20)
    start_button.grid(row=0, column=0, padx=8)
    cancel_button = tk.Button(controls, text="Cancel Recording", width=20, state="disabled")
    cancel_button.grid(row=0, column=1, padx=8)

    active = [False]
    phase = [None]
    phase_end = [None]
    record_start = [None]
    record_end = [None]
    iteration = [0]
    total_iterations = [0]
    planned_sample_ids = []
    person_id = [None]
    gesture_name = [None]

    def set_inputs_enabled(enabled):
        state = "normal" if enabled else "disabled"
        for widget in (person_entry, gesture_menu, count_entry):
            widget.configure(state=state)

    def save_current_iteration():
        sample_id = planned_sample_ids[iteration[0] - 1]
        path = RECORDING_DIRS[gesture_name[0]] / (
            f"{person_id[0]}_{gesture_name[0]}_{sample_id}.csv")
        with lock:
            left_snapshot = list(left_samples)
            right_snapshot = list(right_samples)
        pair_count = save_aligned_samples(
            path, left_snapshot, right_snapshot, record_start[0], record_end[0])
        status_var.set(f"Sample {sample_id} saved — {pair_count} paired rows in {path.as_posix()}")

    def finish_recording():
        active[0] = False
        phase[0] = None
        phase_end[0] = None
        set_inputs_enabled(True)
        start_button.configure(state="normal")
        cancel_button.configure(state="disabled")
        phase_var.set("ALL RECORDINGS FINISHED")
        countdown_var.set("")
        status_var.set(
            f"Saved {total_iterations[0]} {gesture_name[0]} sample(s) in "
            f"./{RECORDING_DIRS[gesture_name[0]].as_posix()}/")

    def start_recording():
        if active[0]:
            return
        with connection_lock:
            connected = active_serials["left"] is not None and active_serials["right"] is not None
        if not connected:
            status_var.set("Waiting for both gloves to connect before recording.")
            return
        try:
            count = int(count_var.get().strip())
            if count <= 0:
                raise ValueError
            person = person_var.get().strip().upper()
            if not person.startswith("P") or not person[1:].isdigit():
                raise ValueError("Person ID must look like P02.")
            gesture = gesture_var.get().strip().upper()
            if gesture not in GESTURES:
                raise ValueError("Choose IDLE or MOVING.")
        except ValueError:
            messagebox.showerror("Invalid recording settings",
                                 "Enter a positive recording count, a person ID such as P02, "
                                 "and choose IDLE or MOVING.", parent=root)
            return

        directory = RECORDING_DIRS[gesture]
        directory.mkdir(parents=True, exist_ok=True)
        planned_ids = next_sample_ids(person, gesture, count)
        existing = [directory / f"{person}_{gesture}_{sample_id}.csv" for sample_id in planned_ids]
        existing = [path.name for path in existing if path.exists()]
        if existing:
            messagebox.showerror("Files already exist",
                                 "No files will be overwritten:\n" + existing[0], parent=root)
            return

        active[0] = True
        iteration[0] = 1
        total_iterations[0] = count
        planned_sample_ids[:] = planned_ids
        person_id[0] = person
        gesture_name[0] = gesture
        phase[0] = "wait"
        phase_end[0] = time.perf_counter() + WAIT_SECONDS
        set_inputs_enabled(False)
        start_button.configure(state="disabled")
        cancel_button.configure(state="normal")

    def cancel_recording():
        if not active[0]:
            return
        active[0] = False
        phase[0] = None
        phase_end[0] = None
        record_start[0] = None
        record_end[0] = None
        set_inputs_enabled(True)
        start_button.configure(state="normal")
        cancel_button.configure(state="disabled")
        phase_var.set("CANCELLED")
        countdown_var.set("Current sample discarded")
        status_var.set(f"Saved completed samples only; sample {iteration[0]} was not saved.")

    def update_display():
        now = time.perf_counter()
        with lock:
            rows_by_panel = (list(left_rows), list(right_rows))
            states_by_hand = (model_states["left"], model_states["right"])
            current_errors = list(errors)

        for display, rows in zip(panels, rows_by_panel):
            display.configure(state="normal")
            display.delete("1.0", tk.END)
            display.insert("1.0", "\n".join(rows or ["Waiting for sensor data..."]))
            display.configure(state="disabled")
        for indicator, movement_state in zip(movement_indicators, states_by_hand):
            if movement_state == "MOVING":
                indicator.configure(text="MOVING", bg="#d9534f", fg="white")
            elif movement_state == "IDLE":
                indicator.configure(text="IDLE", bg="#5cb85c", fg="white")
            else:
                indicator.configure(text="WAITING", bg="#d9d9d9", fg="#222222")
        if current_errors:
            status_var.set(" | ".join(current_errors))

        if active[0] and phase_end[0] is not None:
            remaining = max(0.0, phase_end[0] - now)
            countdown = max(1, math.ceil(remaining))
            sample_id = planned_sample_ids[iteration[0] - 1]
            if phase[0] == "wait":
                phase_var.set(f"SAMPLE {sample_id} — WAIT ({iteration[0]}/{total_iterations[0]})")
                countdown_var.set(f"Recording starts in {countdown}")
                if remaining <= 0:
                    phase[0] = "record"
                    record_start[0] = phase_end[0]
                    record_end[0] = record_start[0] + RECORD_SECONDS
                    phase_end[0] = record_end[0]
            elif phase[0] == "record":
                phase_var.set(f"SAMPLE {sample_id} — {gesture_name[0]} ({iteration[0]}/{total_iterations[0]})")
                countdown_var.set(f"Recording for {countdown}")
                if remaining <= 0:
                    save_current_iteration()
                    if iteration[0] >= total_iterations[0]:
                        finish_recording()
                    else:
                        phase[0] = "wait"
                        phase_end[0] = record_end[0] + WAIT_SECONDS
                        iteration[0] += 1
                        record_start[0] = None
                        record_end[0] = None

        if not stop_event.is_set():
            root.after(50, update_display)

    def connection_manager():
        while not stop_event.is_set():
            result = connect_until_both_ready(stop_event)
            if result is None:
                return
            left_port, left_serial, right_port, right_serial = result
            with connection_lock:
                active_serials["left"] = left_serial
                active_serials["right"] = right_serial
            with lock:
                errors[:] = [f"Both gloves connected — LEFT {left_port}, RIGHT {right_port}."]
            worker_stop = threading.Event()
            workers = [
                threading.Thread(target=serial_worker,
                                 args=("LEFT", left_port, left_serial, left_rows, left_samples,
                                       model_states, lock, worker_stop, errors), daemon=True),
                threading.Thread(target=serial_worker,
                                 args=("RIGHT", right_port, right_serial, right_rows, right_samples,
                                       model_states, lock, worker_stop, errors), daemon=True),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            with connection_lock:
                active_serials["left"] = None
                active_serials["right"] = None
            with lock:
                model_states["left"] = "WAITING"
                model_states["right"] = "WAITING"
            if not stop_event.is_set():
                with lock:
                    errors.append("Connection lost — retrying both gloves.")

    def close():
        stop_event.set()
        with connection_lock:
            for key in ("left", "right"):
                ser = active_serials[key]
                if ser is not None:
                    try:
                        ser.close()
                    except (serial.SerialException, OSError):
                        pass
        root.destroy()

    start_button.configure(command=start_recording)
    cancel_button.configure(command=cancel_recording)
    root.protocol("WM_DELETE_WINDOW", close)
    connection_thread = threading.Thread(target=connection_manager, daemon=True)
    connection_thread.start()
    update_display()
    root.mainloop()
    stop_event.set()
    connection_thread.join(timeout=2)


if __name__ == "__main__":
    main()
