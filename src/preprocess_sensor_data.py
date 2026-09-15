from pathlib import Path
import re
import argparse
import numpy as np
import pandas as pd

FEATURE_COLUMNS = [
    "ax_g", "ay_g", "az_g",
    "gx_deg_s", "gy_deg_s", "gz_deg_s",
    "yaw_deg", "pitch_deg", "roll_deg",
    "flex_1", "flex_2",
]

CLASS_NAMES = [
    "PICKUPHOLD",
    "PLACEDOWN",
    "CHOP",
    "WASH",
    "THROW",
    "FIREEXT",
]

WINDOW_SIZE = 50


def load_window(path):
    df = pd.read_csv(path)

    missing = [col for col in FEATURE_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"{path} is missing columns: {missing}")

    df = df[FEATURE_COLUMNS].apply(pd.to_numeric, errors="coerce")
    df = df.interpolate().bfill().ffill()

    if len(df) < 2:
        raise ValueError(f"{path} does not contain enough samples")

    # Resample every trial to exactly 50 time steps.
    old_positions = np.linspace(0, 1, len(df))
    new_positions = np.linspace(0, 1, WINDOW_SIZE)

    output = np.zeros((WINDOW_SIZE, len(FEATURE_COLUMNS)), dtype=np.float32)

    for i, column in enumerate(FEATURE_COLUMNS):
        output[:, i] = np.interp(
            new_positions,
            old_positions,
            df[column].to_numpy(dtype=np.float32),
        )

    return output


def parse_filename(path):
    # Expected:
    # P01_CHOP_LEFT_01_timestamped.csv
    pattern = r"^(P\d+)_([A-Z0-9]+)_(LEFT|RIGHT)_(\d+)_TIMESTAMPED\.CSV$"
    match = re.match(pattern, path.name.upper())

    if not match:
        return None

    person, gesture, hand, trial = match.groups()

    if gesture not in CLASS_NAMES:
        return None

    return person, gesture, hand, trial


def build_dataset(input_dir):
    input_dir = Path(input_dir)
    files = list(input_dir.glob("*.csv"))

    pairs = {}

    for path in files:
        parsed = parse_filename(path)

        if parsed is None:
            print(f"Skipping incorrectly named file: {path.name}")
            continue

        person, gesture, hand, trial = parsed
        key = (person, gesture, trial)

        if key not in pairs:
            pairs[key] = {}

        pairs[key][hand] = path

    X = []
    y = []
    metadata = []

    for key, hands in sorted(pairs.items()):
        person, gesture, trial = key

        if "LEFT" not in hands or "RIGHT" not in hands:
            print(f"Skipping incomplete pair: {key}")
            continue

        left = load_window(hands["LEFT"])
        right = load_window(hands["RIGHT"])

        # Shape: (50, 22)
        combined = np.concatenate([left, right], axis=1)

        X.append(combined)
        y.append(CLASS_NAMES.index(gesture))
        metadata.append((person, gesture, trial))

    if not X:
        raise RuntimeError("No complete left/right pairs were found.")

    return (
        np.asarray(X, dtype=np.float32),
        np.asarray(y, dtype=np.int64),
        metadata,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input-dir",
        default="data/real",
        help="Folder containing labelled CSV files",
    )
    args = parser.parse_args()

    X, y, metadata = build_dataset(args.input_dir)

    output_dir = Path("data/real_processed")
    output_dir.mkdir(parents=True, exist_ok=True)

    np.save(output_dir / "X.npy", X)
    np.save(output_dir / "y.npy", y)

    pd.DataFrame(
        metadata,
        columns=["person", "gesture", "trial"],
    ).to_csv(output_dir / "metadata.csv", index=False)

    with open(output_dir / "class_names.txt", "w") as file:
        file.write("\n".join(CLASS_NAMES))

    print("Real dataset created successfully")
    print("X shape:", X.shape)
    print("y shape:", y.shape)
    print("Number of complete pairs:", len(X))
    print("Classes:", CLASS_NAMES)


if __name__ == "__main__":
    main()