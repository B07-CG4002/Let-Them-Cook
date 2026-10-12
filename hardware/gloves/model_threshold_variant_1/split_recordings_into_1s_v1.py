"""Split paired two-hand recordings into one-second, per-hand CSV files.

Examples:
    python split_recordings_into_1s_v1.py recordings/idle/P00_IDLE_1.csv
    python split_recordings_into_1s_v1.py recordings/idle recordings/moving
    python split_recordings_into_1s_v1.py recordings/idle recordings/moving

Each input writes into LEFT and RIGHT subfolders beside the input CSV (or
under --output-dir/<input-folder-name>/). The class/person/sample ID in the
input filename is retained in each output filename.
"""
import argparse
import csv
import math
from pathlib import Path
import sys

META_COLUMNS = ("timestamp_utc", "elapsed_s", "sample_index")
HAND_PREFIXES = ("left_", "right_")


def read_recording(path):
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV is empty or has no header")
        fields = reader.fieldnames
        missing = [name for name in META_COLUMNS if name not in fields]
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
                raise ValueError(f"elapsed_s must be finite and non-negative (line {line_number})")
            rows.append((elapsed, row))

    if not rows:
        raise ValueError("CSV contains no data rows")
    return fields, hand_columns, rows


def split_recording(input_path, output_root, segment_seconds=1.0):
    fields, hand_columns, rows = read_recording(input_path)
    max_elapsed = max(elapsed for elapsed, _ in rows)
    segment_count = max(1, math.ceil((max_elapsed + 1e-12) / segment_seconds))
    segments = [[[], []] for _ in range(segment_count)]

    for elapsed, row in rows:
        segment_index = min(int(elapsed // segment_seconds), segment_count - 1)
        segments[segment_index][0].append((elapsed, row))
        segments[segment_index][1].append((elapsed, row))

    input_stem = input_path.stem
    output_dir = output_root
    for segment_index, (left_rows, right_rows) in enumerate(segments, start=1):
        for hand, hand_rows in (("LEFT", left_rows), ("RIGHT", right_rows)):
            hand_dir = output_dir / hand.lower()
            hand_dir.mkdir(parents=True, exist_ok=True)
            output_path = hand_dir / f"{input_stem}_{hand}_1s_{segment_index:02d}.csv"
            columns = [*META_COLUMNS, *hand_columns[hand]]
            start_elapsed = (segment_index - 1) * segment_seconds
            with output_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                for sample_index, (_, row) in enumerate(hand_rows, start=1):
                    out_row = {column: row[column] for column in columns}
                    # Each one-second file starts at elapsed time zero.
                    out_row["elapsed_s"] = f"{max(0.0, float(row['elapsed_s']) - start_elapsed):.6f}"
                    # Reset sample_index within each small file.
                    out_row["sample_index"] = sample_index
                    writer.writerow(out_row)

    return output_dir, segment_count, len(rows)


def collect_inputs(paths):
    files = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            files.extend(sorted(child for child in path.glob("*.csv") if child.is_file()))
        elif path.is_file() and path.suffix.lower() == ".csv":
            files.append(path)
        else:
            raise ValueError(f"Not a CSV file or directory: {path}")
    unique_files = list(dict.fromkeys(path.resolve() for path in files))
    if not unique_files:
        raise ValueError("No CSV files found")
    return unique_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="CSV file(s) or directories containing CSV files")
    parser.add_argument("--output-dir", type=Path,
                        help="Parent output folder (default: next to each input CSV)")
    parser.add_argument("--seconds", type=float, default=1.0,
                        help="Length of each segment in seconds (default: 1.0)")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be a positive number")

    try:
        inputs = collect_inputs(args.inputs)
        for input_path in inputs:
            if args.output_dir:
                # Keep class folders separate when idle and moving inputs are
                # processed together into one chosen output root.
                output_root = args.output_dir / input_path.parent.name
            else:
                output_root = input_path.parent
            output_dir, segment_count, row_count = split_recording(
                input_path, output_root, args.seconds)
            print(f"{input_path}: {row_count} paired rows -> {segment_count} windows in {output_dir}")
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
