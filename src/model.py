import torch.nn as nn

class GestureCNN(nn.Module):
    def __init__(self, num_classes=6, num_features=11):
        super().__init__()

        self.features = nn.Sequential(
            nn.Conv1d(num_features, 32, kernel_size=3),
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