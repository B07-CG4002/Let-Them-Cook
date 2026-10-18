"""Split paired gesture recordings into equal-duration LEFT and RIGHT windows.

Examples:
    python split_gesture_recordings_into_windows_v1.py recordings/wave --window-seconds 2
    python split_gesture_recordings_into_windows_v1.py recordings/wave recordings/grab --window-seconds 1

Input folders are scanned for CSV files directly inside them. Existing left/
right output folders are not scanned, so rerunning the command does not
attempt to split the generated files again.
"""
import argparse
import csv
import math
from pathlib import Path
import sys

META_COLUMNS = ("timestamp_utc", "elapsed_s", "sample_index")


def read_recording(path):
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV is empty or has no header")

        fields = reader.fieldnames
        missing = [column for column in META_COLUMNS if column not in fields]
        if missing:
            raise ValueError("Missing required column(s): " + ", ".join(missing))

        hand_columns = {
            hand: [name for name in fields if name.startswith(hand.lower() + "_")]
            for hand in ("LEFT", "RIGHT")
        }
        if not hand_columns["LEFT"] or not hand_columns["RIGHT"]:
            raise ValueError("Expected both left_* and right_* sensor columns")

        rows = []
        for line_number, row in enumerate(reader, start=2):
            if not row or not any(value not in (None, "") for value in row.values()):
                continue
            try:
                elapsed = float(row["elapsed_s"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid elapsed_s on CSV line {line_number}") from exc
            if not math.isfinite(elapsed) or elapsed < 0:
                raise ValueError(
                    f"elapsed_s must be finite and non-negative (line {line_number})"
                )
            rows.append((elapsed, row))

    if len(rows) < 2:
        raise ValueError("Need at least two data rows to infer the recording duration")

    rows.sort(key=lambda item: item[0])
    positive_steps = [
        later[0] - earlier[0]
        for earlier, later in zip(rows, rows[1:])
        if later[0] > earlier[0]
    ]
    if not positive_steps:
        raise ValueError("elapsed_s does not advance; cannot infer the recording duration")

    # The recording script does not store the requested duration in the CSV.
    # Estimate it as the last elapsed time plus one typical packet interval.
    packet_interval = sorted(positive_steps)[len(positive_steps) // 2]
    estimated_duration = rows[-1][0] + packet_interval
    return fields, hand_columns, rows, estimated_duration, packet_interval


def plan_windows(path, window_seconds):
    fields, hand_columns, rows, duration, packet_interval = read_recording(path)
    window_count = round(duration / window_seconds)
    if window_count < 1:
        raise ValueError(
            f"estimated recording duration {duration:.3f}s is shorter than "
            f"the requested window {window_seconds:g}s"
        )

    remainder = abs(duration - window_count * window_seconds)
    # Packet timestamps are sampled discretely, so allow a small boundary
    # tolerance while still rejecting a genuine leftover interval.
    tolerance = max(0.05, 2.5 * packet_interval)
    if remainder > tolerance:
        raise ValueError(
            f"estimated recording duration is {duration:.3f}s, which cannot be "
            f"split evenly into {window_seconds:g}s windows "
            f"(remainder about {remainder:.3f}s)"
        )

    expected_end = window_count * window_seconds
    if rows[-1][0] > expected_end:
        raise ValueError(
            f"the last sensor row is at {rows[-1][0]:.3f}s, beyond the "
            f"{expected_end:g}s boundary for {window_count} equal windows"
        )

    segments = [[[], []] for _ in range(window_count)]
    for elapsed, row in rows:
        segment_index = min(int(elapsed // window_seconds), window_count - 1)
        segments[segment_index][0].append((elapsed, row))
        segments[segment_index][1].append((elapsed, row))

    for index, (left_rows, right_rows) in enumerate(segments, start=1):
        if not left_rows or not right_rows:
            raise ValueError(
                f"window {index} has no paired sensor rows; cannot make complete splits"
            )

    return fields, hand_columns, segments, duration


def duration_tag(seconds):
    text = f"{seconds:g}".replace(".", "p")
    return f"{text}s"


def split_recording(path, window_seconds, plan):
    _, hand_columns, segments, _ = plan
    tag = duration_tag(window_seconds)
    written = []

    for segment_index, segment in enumerate(segments, start=1):
        for hand, hand_rows in (("LEFT", segment[0]), ("RIGHT", segment[1])):
            hand_dir = path.parent / hand.lower()
            hand_dir.mkdir(parents=True, exist_ok=True)
            output_path = hand_dir / (
                f"{path.stem}_{hand}_{tag}_{segment_index:02d}.csv"
            )

            columns = [*META_COLUMNS, *hand_columns[hand]]
            segment_start = (segment_index - 1) * window_seconds
            with output_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                for sample_index, (elapsed, row) in enumerate(hand_rows, start=1):
                    out_row = {column: row[column] for column in columns}
                    out_row["elapsed_s"] = f"{max(0.0, elapsed - segment_start):.6f}"
                    out_row["sample_index"] = sample_index
                    writer.writerow(out_row)
            written.append(output_path)

    return written


def collect_inputs(paths):
    files = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            # Deliberately non-recursive: generated left/right files are skipped.
            files.extend(sorted(child for child in path.glob("*.csv") if child.is_file()))
        elif path.is_file() and path.suffix.lower() == ".csv":
            files.append(path)
        else:
            raise ValueError(f"Not a CSV file or directory: {path}")

    unique_files = list(dict.fromkeys(path.resolve() for path in files))
    if not unique_files:
        raise ValueError("No source CSV files found directly in the input folder(s)")
    return unique_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs",
        nargs="+",
        help="paired CSV file(s) or folders containing paired CSV files",
    )
    parser.add_argument(
        "--window-seconds",
        type=float,
        required=True,
        help="duration of each output window in seconds",
    )
    args = parser.parse_args()
    if not math.isfinite(args.window_seconds) or args.window_seconds <= 0:
        parser.error("--window-seconds must be a positive number")

    try:
        inputs = collect_inputs(args.inputs)
        # Validate every input before creating or overwriting any split files.
        plans = []
        errors = []
        for path in inputs:
            try:
                plans.append((path, plan_windows(path, args.window_seconds)))
            except (OSError, ValueError, csv.Error) as exc:
                errors.append((path, exc))

        if errors:
            for path, exc in errors:
                print(f"Cannot split {path}: {exc}", file=sys.stderr)
            print("No split files were written.", file=sys.stderr)
            return 1

        for path, plan in plans:
            written = split_recording(path, args.window_seconds, plan)
            _, _, segments, duration = plan
            print(
                f"{path}: about {duration:.3f}s -> {len(segments)} "
                f"{args.window_seconds:g}s windows; wrote {len(written)} files "
                f"under {path.parent / 'left'} and {path.parent / 'right'}"
            )
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
