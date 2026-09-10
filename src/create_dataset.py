import numpy as np
from pathlib import Path


CLASS_NAMES = [
    "PICK_UP_HOLD",
    "PLACE_DOWN",
    "CHOP",
    "WASH",
    "THROW",
    "USE_FIRE_EXTINGUISHER",
]

WINDOW_SIZE = 50
NUM_FEATURES = 16
SAMPLES_PER_CLASS = 100


def main():
    rng = np.random.default_rng(42)

    all_windows = []
    all_labels = []

    for class_id in range(len(CLASS_NAMES)):
        for _ in range(SAMPLES_PER_CLASS):
            window = rng.normal(
                loc=0.0,
                scale=0.5,
                size=(WINDOW_SIZE, NUM_FEATURES),
            )

            # Add a simple class-specific pattern
            window[:, class_id % NUM_FEATURES] += 2.0

            all_windows.append(window.astype(np.float32))
            all_labels.append(class_id)

    X = np.array(all_windows, dtype=np.float32)
    y = np.array(all_labels, dtype=np.int64)

    # Shuffle the dataset
    indices = rng.permutation(len(X))
    X = X[indices]
    y = y[indices]

    output_folder = Path("data/simulated")
    output_folder.mkdir(parents=True, exist_ok=True)

    np.save(output_folder / "X.npy", X)
    np.save(output_folder / "y.npy", y)

    print("Dataset created successfully")
    print("X shape:", X.shape)
    print("y shape:", y.shape)
    print("Classes:", CLASS_NAMES)


if __name__ == "__main__":
    main()