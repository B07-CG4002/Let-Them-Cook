import json
from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
)
from torch.utils.data import DataLoader, TensorDataset, random_split

from src.model import GestureCNN


def main():
    class_names_path = Path("models/class_names.json")
    model_path = Path("models/gesture_cnn.pth")
    features_path = Path("data/simulated/X.npy")
    labels_path = Path("data/simulated/y.npy")

    required_files = {
        "class names": class_names_path,
        "model weights": model_path,
        "feature data": features_path,
        "label data": labels_path,
    }

    for description, path in required_files.items():
        if not path.exists():
            raise SystemExit(
                f"Missing {description} file: {path}. "
                "Run the required preparation or training script first."
            )

    with open(class_names_path, "r") as file:
        class_names = json.load(file)

    X = np.load(features_path)
    y = np.load(labels_path)

    # Convert from:
    # (number of windows, 50 samples, 22 features)
    # to:
    # (number of windows, 22 features, 50 samples)
    X = torch.tensor(X, dtype=torch.float32).permute(0, 2, 1)
    y = torch.tensor(y, dtype=torch.long)

    dataset = TensorDataset(X, y)

    train_size = int(0.8 * len(dataset))
    test_size = len(dataset) - train_size

    _, test_dataset = random_split(
        dataset,
        [train_size, test_size],
        generator=torch.Generator().manual_seed(42),
    )

    test_loader = DataLoader(
        test_dataset,
        batch_size=32,
        shuffle=False,
    )

    model = GestureCNN(
        num_classes=len(class_names),
        num_features=X.shape[1],
    )

    # Initialise LazyLinear before loading the trained weights
    with torch.no_grad():
        model(torch.randn(1, X.shape[1], X.shape[2]))

    try:
        state_dict = torch.load(
            model_path,
            map_location="cpu",
            weights_only=True,
        )
    except TypeError:
        # Compatibility fallback for older PyTorch versions
        state_dict = torch.load(
            model_path,
            map_location="cpu",
        )

    model.load_state_dict(state_dict)
    model.eval()

    true_labels = []
    predicted_labels = []

    with torch.no_grad():
        for inputs, labels in test_loader:
            outputs = model(inputs)
            predictions = torch.argmax(outputs, dim=1)

            true_labels.extend(labels.numpy())
            predicted_labels.extend(predictions.numpy())

    accuracy = accuracy_score(true_labels, predicted_labels)

    print(f"Test accuracy: {accuracy:.2%}")

    print("\nClassification report:")
    print(
        classification_report(
            true_labels,
            predicted_labels,
            labels=range(len(class_names)),
            target_names=class_names,
            zero_division=0,
        )
    )

    print("Confusion matrix:")
    print(
        confusion_matrix(
            true_labels,
            predicted_labels,
            labels=range(len(class_names)),
        )
    )


if __name__ == "__main__":
    main()