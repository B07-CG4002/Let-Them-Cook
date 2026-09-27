"""Preprocess paired glove trials without modifying baseline processed data.

Example:
  python -m src.preprocess_sensor_data --input-dirs data/real/P02 --output-dir data/person2_processed
  python -m src.preprocess_sensor_data --input-dirs data/real/P01 data/real/P02 --output-dir data/combined_processed
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

FEATURE_COLUMNS = [
    "ax_g", "ay_g", "az_g", "gx_deg_s", "gy_deg_s", "gz_deg_s",
    "yaw_deg", "pitch_deg", "roll_deg", "flex_1", "flex_2",
]
CLASS_NAMES = ["PICKUPHOLD", "PLACEDOWN", "CHOP", "WASH", "THROW", "FIREEXT"]
WINDOW_SIZE = 50
FILE_PATTERN = re.compile(
    r"^(P\d+)_([A-Z0-9]+)_(LEFT|RIGHT)_(\d+)_TIMESTAMPED\.CSV$",
    re.IGNORECASE,
)


def parse_file(path: Path):
    match = FILE_PATTERN.fullmatch(path.name)
    if not match:
        return None
    person, gesture, hand, trial = match.groups()
    person, gesture, hand = person.upper(), gesture.upper(), hand.upper()
    if gesture not in CLASS_NAMES:
        raise ValueError(f"Unknown gesture '{gesture}' in {path}")
    return person, gesture, int(trial), hand


def load_resampled(path: Path) -> np.ndarray:
    frame = pd.read_csv(path)
    missing = [column for column in FEATURE_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"{path} is missing columns: {missing}")
    values = frame[FEATURE_COLUMNS].apply(pd.to_numeric, errors="coerce")
    if len(values) < 2:
        raise ValueError(f"{path} contains fewer than two samples")
    if values.isna().any().any():
        bad = int(values.isna().sum().sum())
        raise ValueError(f"{path} contains {bad} missing or nonnumeric sensor values")
    array = values.to_numpy(dtype=np.float32)
    if not np.isfinite(array).all():
        raise ValueError(f"{path} contains non-finite sensor values")
    old = np.linspace(0.0, 1.0, len(array))
    new = np.linspace(0.0, 1.0, WINDOW_SIZE)
    return np.column_stack([
        np.interp(new, old, array[:, feature])
        for feature in range(array.shape[1])
    ]).astype(np.float32)


def build_dataset(input_dirs):
    pairs = defaultdict(dict)
    for directory in map(Path, input_dirs):
        if not directory.is_dir():
            raise FileNotFoundError(f"Input folder does not exist: {directory}")
        for path in directory.rglob("*.csv"):
            parsed = parse_file(path)
            if parsed is None:
                print(f"Skipping unrecognized filename: {path}")
                continue
            person, gesture, trial, hand = parsed
            key = (person, gesture, trial)
            if hand in pairs[key]:
                raise ValueError(f"Duplicate {hand} file for trial {key}: {path}")
            pairs[key][hand] = path

    if not pairs:
        raise RuntimeError("No correctly named labelled trial files found.")

    X, y, rows = [], [], []
    incomplete = []
    for key in sorted(pairs):
        hands = pairs[key]
        if set(hands) != {"LEFT", "RIGHT"}:
            incomplete.append((key, sorted(hands)))
            continue
        left = load_resampled(hands["LEFT"])
        right = load_resampled(hands["RIGHT"])
        X.append(np.concatenate((left, right), axis=1))
        y.append(CLASS_NAMES.index(key[1]))
        rows.append({"person": key[0], "gesture": key[1], "trial": key[2],
                     "class_id": CLASS_NAMES.index(key[1])})

    if incomplete:
        details = "; ".join(f"{key}: {hands}" for key, hands in incomplete[:10])
        raise RuntimeError(f"Found incomplete left/right pairs ({len(incomplete)}): {details}")
    return np.asarray(X, dtype=np.float32), np.asarray(y, dtype=np.int64), pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dirs", nargs="+", required=True,
                        help="One or more folders containing labelled paired CSV trials")
    parser.add_argument("--output-dir", required=True,
                        help="New processed output folder; existing output files are protected")
    args = parser.parse_args()
    output = Path(args.output_dir)
    protected = [output / name for name in ("X.npy", "y.npy", "metadata.csv", "class_names.txt")]
    existing = [path for path in protected if path.exists()]
    if existing:
        raise SystemExit("Refusing to overwrite existing processed files: " + ", ".join(map(str, existing)))

    X, y, metadata = build_dataset(args.input_dirs)
    output.mkdir(parents=True, exist_ok=True)
    np.save(output / "X.npy", X)
    np.save(output / "y.npy", y)
    metadata.to_csv(output / "metadata.csv", index=False)
    (output / "class_names.txt").write_text("\n".join(CLASS_NAMES) + "\n", encoding="utf-8")
    print(f"Processed {len(X)} paired trials")
    print(f"X shape: {X.shape}; y shape: {y.shape}")
    print(metadata.groupby(["person", "gesture"]).size().to_string())
    print(f"Saved separate output to {output}")


if __name__ == "__main__":
    main()
