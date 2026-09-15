from pathlib import Path
from collections import deque
import json

import numpy as np
import torch

from src.model import GestureCNN


WINDOW_SIZE = 50
NUM_FEATURES = 22
MODEL_PATH = Path("models/gesture_cnn.pth")
CLASS_NAMES_PATH = Path("models/class_names.json")


def load_model():
    with open(CLASS_NAMES_PATH, "r") as file:
        class_names = json.load(file)

    model = GestureCNN(
        num_features=NUM_FEATURES,
        num_classes=len(class_names),
    )

    model.load_state_dict(torch.load(MODEL_PATH, map_location="cpu"))
    model.eval()

    return model, class_names


def predict(model, class_names, window):
    # Model expects: batch, features, time
    input_data = np.asarray(window, dtype=np.float32)
    input_tensor = torch.tensor(input_data).transpose(0, 1).unsqueeze(0)

    with torch.no_grad():
        output = model(input_tensor)
        probabilities = torch.softmax(output, dim=1)
        confidence, prediction = torch.max(probabilities, dim=1)

    return (
        class_names[prediction.item()],
        confidence.item(),
    )


def main():
    model, class_names = load_model()
    window = deque(maxlen=WINDOW_SIZE)

    print("Live inference started.")
    print("Waiting for 22-feature samples...")

    # Temporary test data.
    # Replace this section with ESP32 serial input later.
    for _ in range(WINDOW_SIZE):
        sample = np.zeros(NUM_FEATURES, dtype=np.float32)
        window.append(sample)

    gesture, confidence = predict(model, class_names, window)

    print({
        "gesture": gesture,
        "confidence": round(confidence, 4),
        "status": "VALID" if confidence >= 0.70 else "LOW_CONFIDENCE",
    })


if __name__ == "__main__":
    main()