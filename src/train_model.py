import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset, random_split

from src.model import GestureCNN


CLASS_NAMES = [
    "PICKUPHOLD",
    "PLACEDOWN",
    "CHOP",
    "WASH",
    "THROW",
    "FIREEXT",
]


def main():
    X = np.load("data/real_processed/X.npy")
    y = np.load("data/real_processed/y.npy")

    # Convert:
    # (number of windows, 50 samples, 22 features)
    # to:
    # (number of windows, 22 features, 50 samples)
    X = torch.tensor(X, dtype=torch.float32).permute(0, 2, 1)
    y = torch.tensor(y, dtype=torch.long)

    dataset = TensorDataset(X, y)

    train_size = int(0.8 * len(dataset))
    test_size = len(dataset) - train_size

    train_dataset, test_dataset = random_split(
        dataset,
        [train_size, test_size],
        generator=torch.Generator().manual_seed(42),
    )

    train_loader = DataLoader(
        train_dataset,
        batch_size=32,
        shuffle=True,
    )

    test_loader = DataLoader(
        test_dataset,
        batch_size=32,
    )

    model = GestureCNN(
        num_classes=len(CLASS_NAMES),
        num_features=X.shape[1],
    )

    loss_function = nn.CrossEntropyLoss()

    optimizer = torch.optim.Adam(
        model.parameters(),
        lr=0.001,
    )

    for epoch in range(20):
        model.train()
        total_loss = 0.0

        for inputs, labels in train_loader:
            optimizer.zero_grad()

            outputs = model(inputs)
            loss = loss_function(outputs, labels)

            loss.backward()
            optimizer.step()

            total_loss += loss.item()

        average_loss = total_loss / len(train_loader)

        print(
            f"Epoch {epoch + 1}/20 "
            f"- Loss: {average_loss:.4f}"
        )

    model.eval()

    correct = 0
    total = 0

    with torch.no_grad():
        for inputs, labels in test_loader:
            outputs = model(inputs)
            predictions = torch.argmax(outputs, dim=1)

            correct += (predictions == labels).sum().item()
            total += labels.size(0)

    accuracy = correct / total

    print(f"Test accuracy: {accuracy:.2%}")

    output_folder = Path("models")
    output_folder.mkdir(parents=True, exist_ok=True)

    torch.save(
        model.state_dict(),
        output_folder / "gesture_cnn.pth",
    )

    with open(output_folder / "class_names.json", "w") as file:
        json.dump(CLASS_NAMES, file, indent=2)

    print("Model saved to models/gesture_cnn.pth")


if __name__ == "__main__":
    main()