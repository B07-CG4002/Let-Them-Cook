import json
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import TensorDataset, DataLoader, random_split

CLASS_NAMES = [
    "PICK_UP_HOLD",
    "PLACE_DOWN",
    "CHOP",
    "WASH",
    "THROW",
    "USE_FIRE_EXTINGUISHER",
]

class GestureCNN(nn.Module):
    def __init__(self, num_classes=6):
        super().__init__()

        self.features = nn.Sequential(
            nn.Conv1d(16, 32, kernel_size=3),
            nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Conv1d(32, 64, kernel_size=3),
            nn.ReLU(),
            nn.MaxPool1d(2),
        )

        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.LazyLinear(64),
            nn.ReLU(),
            nn.Linear(64, num_classes),
        )

    def forward(self, x):
        return self.classifier(self.features(x))


X = np.load("data/simulated/X.npy")
y = np.load("data/simulated/y.npy")

# Convert: (samples, 50, 16) → (samples, 16, 50)
X = torch.tensor(X, dtype=torch.float32).permute(0, 2, 1)
y = torch.tensor(y, dtype=torch.long)

dataset = TensorDataset(X, y)

train_size = int(0.8 * len(dataset))
test_size = len(dataset) - train_size

train_dataset, test_dataset = random_split(
    dataset,
    [train_size, test_size],
    generator=torch.Generator().manual_seed(42)
)

train_loader = DataLoader(train_dataset, batch_size=32, shuffle=True)
test_loader = DataLoader(test_dataset, batch_size=32)

model = GestureCNN(num_classes=6)
loss_function = nn.CrossEntropyLoss()
optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

for epoch in range(20):
    model.train()
    total_loss = 0

    for inputs, labels in train_loader:
        optimizer.zero_grad()
        outputs = model(inputs)
        loss = loss_function(outputs, labels)
        loss.backward()
        optimizer.step()
        total_loss += loss.item()

    print(
        f"Epoch {epoch + 1}/20 "
        f"- Loss: {total_loss / len(train_loader):.4f}"
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

torch.save(model.state_dict(), "models/gesture_cnn.pth")

with open("models/class_names.json", "w") as file:
    json.dump(CLASS_NAMES, file, indent=2)

print("Model saved to models/gesture_cnn.pth")