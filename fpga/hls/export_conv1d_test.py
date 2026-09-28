"""Export one trained Conv1D layer and one golden case to a raw float32 file.

Run from the repository root in the project's Python environment:
  python fpga/hls/export_conv1d_test.py

Binary order: input [50,22], weight [32,22,3], bias [32], expected [32,48].
"""
from pathlib import Path
import numpy as np
import torch
from src.model import GestureCNN

MODEL = Path("models/gesture_cnn_person1_person2.pth")
VECTORS = Path("fpga/validation/person1_person2/fpga_golden_test_vectors.npz")
OUT = Path("fpga/hls/conv1d_layer1_test.bin")

CLASS_NAMES = 6
model = GestureCNN(num_features=22, num_classes=CLASS_NAMES)
with torch.no_grad():
    model(torch.zeros(1, 22, 50))
try:
    state = torch.load(MODEL, map_location="cpu", weights_only=True)
except TypeError:
    state = torch.load(MODEL, map_location="cpu")
model.load_state_dict(state)
model.eval()
if not hasattr(model.features[0], "weight"):
    raise TypeError("features[0] is not the expected Conv1d module")

with np.load(VECTORS) as data:
    x = np.asarray(data["inputs"][0], dtype=np.float32)
    expected = np.asarray(data["layer__features_0"][0], dtype=np.float32)
if x.shape != (50, 22) or expected.shape != (32, 48):
    raise ValueError(f"Unexpected golden shapes: {x.shape}, {expected.shape}")

conv = model.features[0]
weight = conv.weight.detach().cpu().numpy().astype(np.float32, copy=False)
bias = conv.bias.detach().cpu().numpy().astype(np.float32, copy=False)
if weight.shape != (32, 22, 3) or bias.shape != (32,):
    raise ValueError(f"Unexpected learned parameter shapes: {weight.shape}, {bias.shape}")
OUT.parent.mkdir(parents=True, exist_ok=True)
with OUT.open("wb") as f:
    for a in (x, weight, bias, expected):
        np.ascontiguousarray(a, dtype="<f4").tofile(f)
print(f"Wrote {OUT} ({OUT.stat().st_size} bytes) using held-out sample 0")
print("Float comparison tolerance in testbench: atol=1e-3, rtol=1e-4")
