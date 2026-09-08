import torch
import torch.nn as nn

class GestureCNN(nn.Module):
    def __init__(self, num_classes=6):
        super().__init__()

        self.features = nn.Sequential(
            nn.Conv1d(in_channels=16, out_channels=32, kernel_size=3),
            nn.ReLU(),
            nn.MaxPool1d(kernel_size=2),

            nn.Conv1d(in_channels=32, out_channels=64, kernel_size=3),
            nn.ReLU(),
            nn.MaxPool1d(kernel_size=2)
        )

        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.LazyLinear(64),
            nn.ReLU(),
            nn.Linear(64, num_classes)
        )

    def forward(self, x):
        x = self.features(x)
        return self.classifier(x)


model = GestureCNN()

# PyTorch Conv1D input format:
# batch size × features × samples
test_input = torch.randn(1, 16, 50)

class_names = [
    "PICK_UP_HOLD",
    "PLACE_DOWN",
    "CHOP",
    "WASH",
    "THROW",
    "USE_FIRE_EXTINGUISHER"
]

model.eval()

with torch.no_grad():
    output = model(test_input)
    probabilities = torch.softmax(output, dim=1)
    predicted_class = torch.argmax(probabilities, dim=1).item()
    confidence = probabilities[0, predicted_class].item()

print("Input shape:", test_input.shape)
print("Output shape:", output.shape)
print("Output:", output)
print("Predicted gesture:", class_names[predicted_class])
print("Confidence:", confidence)