import json
from pathlib import Path

import numpy as np
import torch

from src.model import GestureCNN


def main():
    class_names_path = Path("models/class_names.json")
    model_path = Path("models/gesture_cnn.pth")

    if not class_names_path.exists():
        raise SystemExit(
            "Missing models/class_names.json. "
            "Run 'python -m src.train_model' first."
        )

    if not model_path.exists():
        raise SystemExit(
            "Missing models/gesture_cnn.pth. "
            "Run 'python -m src.train_model' first."
        )

    with open(class_names_path, "r") as file:
        class_names = json.load(file)

    model = GestureCNN(num_classes=len(class_names))

    # Load trained weights
    # Load trained weights (avoid unpickling arbitrary objects when possible)
    try:
        state_dict = torch.load(model_path, map_location="cpu", weights_only=True)
    except TypeError:
        state_dict = torch.load(model_path, map_location="cpu")

    model.load_state_dict(state_dict)

    model.eval()

    X = np.load("data/simulated/X.npy")
    test_window = torch.tensor(X[0], dtype=torch.float32)

    # Convert from 50 × 16 to 1 × 16 × 50
    test_window = test_window.permute(1, 0).unsqueeze(0)

    with torch.no_grad():
        output = model(test_window)
        probabilities = torch.softmax(output, dim=1)
        predicted_class = torch.argmax(probabilities, dim=1).item()
        confidence = probabilities[0, predicted_class].item()

    result = {
        "gesture": class_names[predicted_class],
        "confidence": round(confidence, 4),
        "status": "VALID" if confidence >= 0.70 else "LOW_CONFIDENCE",
    }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()