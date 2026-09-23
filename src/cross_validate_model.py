from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.model_selection import StratifiedKFold
from torch.utils.data import DataLoader, TensorDataset

from src.model import GestureCNN


CLASS_NAMES = [
    "PICKUPHOLD",
    "PLACEDOWN",
    "CHOP",
    "WASH",
    "THROW",
    "FIREEXT",
]

NUM_FEATURES = 22
WINDOW_SIZE = 50
NUM_FOLDS = 5
EPOCHS = 20
BATCH_SIZE = 16


def train_one_fold(X, y, train_indices, test_indices, fold_number):
    torch.manual_seed(42 + fold_number)

    train_indices = torch.tensor(train_indices, dtype=torch.long)
    test_indices = torch.tensor(test_indices, dtype=torch.long)

    train_dataset = TensorDataset(
        X[train_indices],
        y[train_indices],
    )

    test_dataset = TensorDataset(
        X[test_indices],
        y[test_indices],
    )

    train_loader = DataLoader(
        train_dataset,
        batch_size=BATCH_SIZE,
        shuffle=True,
    )

    test_loader = DataLoader(
        test_dataset,
        batch_size=BATCH_SIZE,
        shuffle=False,
    )

    model = GestureCNN(
        num_classes=len(CLASS_NAMES),
        num_features=NUM_FEATURES,
    )

    loss_function = nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(
        model.parameters(),
        lr=0.001,
    )

    for epoch in range(EPOCHS):
        model.train()

        for inputs, labels in train_loader:
            optimizer.zero_grad()

            outputs = model(inputs)
            loss = loss_function(outputs, labels)

            loss.backward()
            optimizer.step()

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

    print(
        f"Fold {fold_number}/{NUM_FOLDS} accuracy: "
        f"{accuracy:.2%}"
    )

    return true_labels, predicted_labels


def main():
    features_path = Path("data/real_processed/X.npy")
    labels_path = Path("data/real_processed/y.npy")

    if not features_path.exists() or not labels_path.exists():
        raise SystemExit(
            "Processed real-data files are missing. Run preprocessing first."
        )

    X = np.load(features_path)
    y = np.load(labels_path)

    if X.shape[1:] != (WINDOW_SIZE, NUM_FEATURES):
        raise ValueError(
            f"Expected X shape (*, {WINDOW_SIZE}, {NUM_FEATURES}), "
            f"but received {X.shape}"
        )

    # Convert from (samples, time, features)
    # to (samples, features, time)
    X = torch.tensor(X, dtype=torch.float32).permute(0, 2, 1)
    y = torch.tensor(y, dtype=torch.long)

    splitter = StratifiedKFold(
        n_splits=NUM_FOLDS,
        shuffle=True,
        random_state=42,
    )

    all_true_labels = []
    all_predicted_labels = []

    for fold_number, (train_indices, test_indices) in enumerate(
        splitter.split(X, y.numpy()),
        start=1,
    ):
        true_labels, predicted_labels = train_one_fold(
            X,
            y,
            train_indices,
            test_indices,
            fold_number,
        )

        all_true_labels.extend(true_labels)
        all_predicted_labels.extend(predicted_labels)

    overall_accuracy = accuracy_score(
        all_true_labels,
        all_predicted_labels,
    )

    print("\nCross-validation accuracy:")
    print(f"{overall_accuracy:.2%}")

    print("\nClassification report:")
    print(
        classification_report(
            all_true_labels,
            all_predicted_labels,
            labels=range(len(CLASS_NAMES)),
            target_names=CLASS_NAMES,
            zero_division=0,
        )
    )

    print("Confusion matrix:")
    print(
        confusion_matrix(
            all_true_labels,
            all_predicted_labels,
            labels=range(len(CLASS_NAMES)),
        )
    )


if __name__ == "__main__":
    main()