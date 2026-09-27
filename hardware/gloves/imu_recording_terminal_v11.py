"""Two-glove sensor display and timed recorder.

Requires pyserial. Each Arduino must send 11 comma-separated values:
ax, ay, az, gx, gy, gz, yaw, pitch, roll, flex1, flex2.
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


# Serial port settings — edit these two values when the glove ports change.
LEFT_PORT = "COM3"    # White wire
RIGHT_PORT = "COM4"   # Pink wire
BAUD = 115200
STARTUP_TIMEOUT_SECONDS = 8.0
MAX_SAMPLE_ALIGNMENT_GAP_SECONDS = 0.035
# Save labelled, paired trials in the repository dataset structure.
# Files are stored in data/real/Pxx/ and can be passed to preprocessing.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RECORDING_DIR = REPOSITORY_ROOT / "data" / "real"
CLASS_NAMES = (
    "PICKUPHOLD",
    "PLACEDOWN",
    "CHOP",
    "WASH",
    "THROW",
    "FIREEXT",
)

DATA_HEADER = [
    "ax_g", "ay_g", "az_g", "gx_deg_s", "gy_deg_s", "gz_deg_s",
    "yaw_deg", "pitch_deg", "roll_deg", "flex_1", "flex_2",
]
# Matches the provided example CSV. sample_index starts at 1 for each file.
CSV_HEADER = ["timestamp_utc", "elapsed_s", "sample_index", *DATA_HEADER]
FIELD_NAMES = (
    "ax", "ay", "az", "gx", "gy", "gz",
    "yaw", "pitch", "roll", "flex1", "flex2",
)


def parse_sample(raw):
    """Return a valid 11-value row, ignoring Arduino status messages."""
    try:
        fields = raw.decode("ascii").strip().split(",")
        if len(fields) != 11:
            return None
        values = tuple(float(value) for value in fields)
        return values if all(math.isfinite(value) for value in values) else None
    except (UnicodeDecodeError, ValueError):
        return None


def format_sample(values):
    return " | ".join(
        f"{name}={value: .4f}" for name, value in zip(FIELD_NAMES, values)
    )


def open_and_reset(port):
    """Open a port and request an ESP32 reset through DTR/RTS."""
    ser = serial.Serial(port, BAUD, timeout=0.05)
    ser.dtr = False
    ser.rts = False
    time.sleep(0.1)
    ser.dtr = True
    ser.rts = True
    return ser


def has_valid_sample(ser):
    """Check whether the port has produced at least one valid sensor row."""
    raw = ser.read_all()
    if not raw:
        return False
    return any(parse_sample(line) is not None for line in raw.splitlines())


def connect_until_both_ready(stop_event):
    """Reset both gloves and retry until both produce valid data or stop."""
    while True:
        if stop_event.is_set():
            return None
        left_serial = None
        right_serial = None
        left_ready = False
        right_ready = False
        try:
            left_serial = open_and_reset(LEFT_PORT)
            right_serial = open_and_reset(RIGHT_PORT)
            deadline = time.perf_counter() + STARTUP_TIMEOUT_SECONDS
            left_ready = False
            right_ready = False

            while time.perf_counter() < deadline:
                left_ready = left_ready or has_valid_sample(left_serial)
                right_ready = right_ready or has_valid_sample(right_serial)
                if left_ready and right_ready:
                    return left_serial, right_serial
                if stop_event.wait(0.01):
                    return None
        except (serial.SerialException, OSError):
            pass
        finally:
            if not (left_serial is not None and right_serial is not None
                    and left_ready and right_ready):
                for ser in (left_serial, right_serial):
                    if ser is not None:
                        try:
                            ser.close()
                        except (serial.SerialException, OSError):
                            pass
                stop_event.wait(0.5)


def serial_worker(port, ser, latest_rows, samples, lock, stop_event, errors):
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
                values = parse_sample(raw)
                if values is None:
                    continue
                received = time.perf_counter()
                timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                with lock:
                    latest_rows.append(format_sample(values))
                    samples.append((received, timestamp, values))

            if len(pending) > 65536:
                pending.clear()
    except (serial.SerialException, OSError) as exc:
        with lock:
            errors.append(f"{port}: {exc}")
        stop_event.set()
    finally:
        ser.close()


def align_samples(left_samples, right_samples, start_time, end_time):
    """Pair samples by timestamp and use one shared timestamp per pair."""
    left = [
        (received, timestamp, values)
        for received, timestamp, values in left_samples
        if start_time <= received <= end_time
    ]
    right = [
        (received, timestamp, values)
        for received, timestamp, values in right_samples
        if start_time <= received <= end_time
    ]

    pairs = []
    left_index = 0
    right_index = 0
    while left_index < len(left) and right_index < len(right):
        left_received, left_timestamp, left_values = left[left_index]
        right_received, right_timestamp, right_values = right[right_index]
        difference = left_received - right_received

        if abs(difference) <= MAX_SAMPLE_ALIGNMENT_GAP_SECONDS:
            # Use the left sample's timestamp for both files so the paired
            # rows contain exactly the same timestamp text.
            pairs.append((left_received, left_timestamp, left_values, right_values))
            left_index += 1
            right_index += 1
        elif difference < 0:
            left_index += 1
        else:
            right_index += 1
    return pairs


def save_aligned_samples(left_path, right_path, left_samples, right_samples,
                         start_time, end_time):
    pairs = align_samples(left_samples, right_samples, start_time, end_time)
    left_path.parent.mkdir(parents=True, exist_ok=True)
    right_path.parent.mkdir(parents=True, exist_ok=True)
    with left_path.open("x", newline="", encoding="utf-8") as left_handle, \
         right_path.open("x", newline="", encoding="utf-8") as right_handle:
        left_writer = csv.writer(left_handle)
        right_writer = csv.writer(right_handle)
        left_writer.writerow(CSV_HEADER)
        right_writer.writerow(CSV_HEADER)
        for sample_index, (received, timestamp, left_values, right_values) in enumerate(pairs, start=1):
            row_prefix = [timestamp, f"{received - start_time:.6f}", sample_index]
            left_writer.writerow([*row_prefix, *left_values])
            right_writer.writerow([*row_prefix, *right_values])
    return len(pairs)


def main():
    stop_event = threading.Event()
    lock = threading.Lock()
    left_rows = deque(maxlen=5)
    right_rows = deque(maxlen=5)
    left_samples = deque(maxlen=10000)
    right_samples = deque(maxlen=10000)
    errors = []
    connection_lock = threading.Lock()
    active_serials = {"left": None, "right": None}

    root = tk.Tk()
    root.title("Two-Glove Sensor Recorder")
    root.geometry("1500x760")
    root.minsize(950, 600)

    phase_var = tk.StringVar(value="READY")
    countdown_var = tk.StringVar(value="Press Start Recording")
    status_var = tk.StringVar(value="Opening window — connecting to both gloves...")
    person_var = tk.StringVar(value="P02")
    gesture_var = tk.StringVar(value=CLASS_NAMES[0])
    count_var = tk.StringVar(value="1")

    phase_label = tk.Label(root, textvariable=phase_var, font=("Segoe UI", 28, "bold"))
    phase_label.pack(pady=(16, 0))
    countdown_label = tk.Label(root, textvariable=countdown_var, font=("Segoe UI", 24))
    countdown_label.pack(pady=(0, 8))
    status_label = tk.Label(root, textvariable=status_var, font=("Segoe UI", 11))
    status_label.pack(pady=(0, 12))

    settings = tk.Frame(root)
    settings.pack(pady=(0, 12))
    tk.Label(settings, text="Person ID (for example P02):").grid(row=0, column=0, padx=5, pady=3, sticky="e")
    person_entry = tk.Entry(settings, textvariable=person_var, width=12)
    person_entry.grid(row=0, column=1, padx=5, pady=3, sticky="w")
    tk.Label(settings, text="Gesture:").grid(row=1, column=0, padx=5, pady=3, sticky="e")
    gesture_menu = tk.OptionMenu(settings, gesture_var, *CLASS_NAMES)
    gesture_menu.grid(row=1, column=1, padx=5, pady=3, sticky="w")
    tk.Label(settings, text="Number of recordings:").grid(row=2, column=0, padx=5, pady=3, sticky="e")
    count_entry = tk.Entry(settings, textvariable=count_var, width=12)
    count_entry.grid(row=2, column=1, padx=5, pady=3, sticky="w")
    filename_preview_var = tk.StringVar()
    tk.Label(settings, textvariable=filename_preview_var, justify="left").grid(
        row=3, column=0, columnspan=2, padx=5, pady=3, sticky="w"
    )

    def update_filename_preview(*_):
        person = person_var.get().strip().upper() or "P02"
        gesture = gesture_var.get().strip().upper() or CLASS_NAMES[0]
        filename_preview_var.set(
            "Files will be saved under data/real/{}/\n".format(person)
            + f"{person}_{gesture}_LEFT_01_timestamped.csv\n"
            + f"{person}_{gesture}_RIGHT_01_timestamped.csv"
        )

    person_var.trace_add("write", update_filename_preview)
    gesture_var.trace_add("write", update_filename_preview)
    update_filename_preview()

    panels = []
    for row, title in enumerate(("Left Hand — COM3", "Right Hand — COM4")):
        frame = tk.Frame(root, padx=8, pady=5)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=title, font=("Segoe UI", 11, "bold")).pack(pady=(0, 3))
        display = tk.Text(
            frame,
            wrap="none",
            height=5,
            state="disabled",
            font=("Consolas", 10),
            padx=8,
            pady=6,
        )
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
    planned_trial_numbers = []
    person_id = [None]
    gesture_name = [None]
    iteration_saved = [False]

    def set_inputs_enabled(enabled):
        state = "normal" if enabled else "disabled"
        for widget in (person_entry, gesture_menu, count_entry):
            widget.configure(state=state)

    def save_current_iteration():
        number = planned_trial_numbers[iteration[0] - 1]
        left_path = (
            RECORDING_DIR / person_id[0]
            / f"{person_id[0]}_{gesture_name[0]}_LEFT_{number:02d}_timestamped.csv"
        )
        right_path = (
            RECORDING_DIR / person_id[0]
            / f"{person_id[0]}_{gesture_name[0]}_RIGHT_{number:02d}_timestamped.csv"
        )
        with lock:
            left_snapshot = list(left_samples)
            right_snapshot = list(right_samples)
        pair_count = save_aligned_samples(
            left_path,
            right_path,
            left_snapshot,
            right_snapshot,
            record_start[0],
            record_end[0],
        )
        iteration_saved[0] = True
        status_var.set(
            f"Trial {number:02d} saved — {pair_count} synchronized samples per hand"
        )

    def start_recording():
        if active[0]:
            return
        with connection_lock:
            connected = (
                active_serials["left"] is not None
                and active_serials["right"] is not None
            )
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
            if gesture not in CLASS_NAMES:
                raise ValueError("Choose one of the listed gesture classes.")
        except ValueError:
            messagebox.showerror(
                "Invalid recording settings",
                "Enter a positive trial count, a person ID such as P02, "
                "and choose a valid gesture.",
                parent=root,
            )
            return

        RECORDING_DIR.mkdir(parents=True, exist_ok=True)
        person_dir = RECORDING_DIR / person
        used_numbers = set()
        if person_dir.exists():
            prefix = f"{person}_{gesture}_"
            for path in person_dir.glob(f"{person}_{gesture}_*_timestamped.csv"):
                parts = path.stem.split("_")
                if len(parts) >= 5 and parts[-2].isdigit():
                    used_numbers.add(int(parts[-2]))

        next_number = 1
        while next_number in used_numbers:
            next_number += 1
        planned_numbers = list(range(next_number, next_number + count))

        expected_paths = []
        for number in planned_numbers:
            expected_paths.extend([
                person_dir / f"{person}_{gesture}_LEFT_{number:02d}_timestamped.csv",
                person_dir / f"{person}_{gesture}_RIGHT_{number:02d}_timestamped.csv",
            ])
        existing = [path.name for path in expected_paths if path.exists()]
        if existing:
            messagebox.showerror(
                "Files already exist",
                "A paired output file already exists. No files will be overwritten:\n"
                + existing[0],
                parent=root,
            )
            return

        active[0] = True
        iteration[0] = 1
        total_iterations[0] = count
        planned_trial_numbers[:] = planned_numbers
        person_id[0] = person
        gesture_name[0] = gesture
        iteration_saved[0] = False
        phase[0] = "wait"
        phase_end[0] = time.perf_counter() + 3.0
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
        countdown_var.set("Current iteration discarded")
        status_var.set(
            f"Completed iterations were kept. No file was saved for iteration {iteration[0]:02d}."
        )

    def update_display():
        now = time.perf_counter()
        with lock:
            rows_by_panel = (list(left_rows), list(right_rows))
            current_errors = list(errors)

        for display, rows in zip(panels, rows_by_panel):
            lines = rows or ["Waiting for sensor data..."]
            display.configure(state="normal")
            display.delete("1.0", tk.END)
            display.insert("1.0", "\n".join(lines))
            display.configure(state="disabled")

        if current_errors:
            status_var.set(" | ".join(current_errors))

        if active[0] and phase_end[0] is not None:
            remaining = max(0.0, phase_end[0] - now)
            countdown = max(1, math.ceil(remaining))

            if phase[0] == "wait":
                trial_number = planned_trial_numbers[iteration[0] - 1]
                phase_var.set(f"TRIAL {trial_number:02d} — WAIT ({iteration[0]}/{total_iterations[0]})")
                countdown_var.set(f"Starting in {countdown}")
                if remaining <= 0:
                    phase[0] = "move"
                    record_start[0] = now
                    record_end[0] = now + 6.0
                    phase_end[0] = record_end[0]
                    iteration_saved[0] = False
            elif phase[0] == "move":
                trial_number = planned_trial_numbers[iteration[0] - 1]
                phase_var.set(f"TRIAL {trial_number:02d} — MOVE NOW ({iteration[0]}/{total_iterations[0]})")
                countdown_var.set(f"Recording for {countdown}")
                if remaining <= 0:
                    save_current_iteration()
                    phase[0] = "rest"
                    phase_end[0] = now + 3.0
                    countdown_var.set("Resting — next recording soon")
            elif phase[0] == "rest":
                trial_number = planned_trial_numbers[iteration[0] - 1]
                phase_var.set(f"TRIAL {trial_number:02d} — FINISHED ({iteration[0]}/{total_iterations[0]})")
                countdown_var.set(f"Next iteration in {countdown}")
                if remaining <= 0:
                    if iteration[0] >= total_iterations[0]:
                        active[0] = False
                        phase[0] = None
                        phase_end[0] = None
                        set_inputs_enabled(True)
                        start_button.configure(state="normal")
                        cancel_button.configure(state="disabled")
                        phase_var.set("ALL RECORDINGS FINISHED")
                        countdown_var.set("")
                        status_var.set(
                            "Saved "
                            f"{total_iterations[0]} left/right recording pairs in "
                            f"data/real/{person_id[0]}/"
                        )
                    else:
                        iteration[0] += 1
                        phase[0] = "wait"
                        phase_end[0] = now + 3.0
                        record_start[0] = None
                        record_end[0] = None

        if not stop_event.is_set():
            root.after(50, update_display)

    def connection_manager():
        """Connect/reconnect in the background while the window stays open."""
        while not stop_event.is_set():
            result = connect_until_both_ready(stop_event)
            if result is None:
                return
            left_serial, right_serial = result
            with connection_lock:
                active_serials["left"] = left_serial
                active_serials["right"] = right_serial
            with lock:
                errors[:] = ["Both gloves connected."]

            worker_stop = threading.Event()
            workers = [
                threading.Thread(
                    target=serial_worker,
                    args=(LEFT_PORT, left_serial, left_rows, left_samples, lock, worker_stop, errors),
                    daemon=True,
                ),
                threading.Thread(
                    target=serial_worker,
                    args=(RIGHT_PORT, right_serial, right_rows, right_samples, lock, worker_stop, errors),
                    daemon=True,
                ),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            with connection_lock:
                active_serials["left"] = None
                active_serials["right"] = None
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
