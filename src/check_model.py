"""Check architecture shapes; optionally load a trained eight-class checkpoint."""
import argparse
import torch
from src.model import GestureCNN
from src.inference import Predictor
p=argparse.ArgumentParser(); p.add_argument('--run-dir'); a=p.parse_args()
model=Predictor(a.run_dir).model if a.run_dir else GestureCNN(num_classes=8,num_features=22)
model.eval()
with torch.no_grad():
    x=torch.zeros(1,22,50)
    print('Input:',tuple(x.shape))
    for layer in model.features:
        x=layer(x); print(type(layer).__name__,tuple(x.shape))
    for layer in model.classifier:
        x=layer(x); print(type(layer).__name__,tuple(x.shape))
print('Loaded trained weights' if a.run_dir else 'Shape check only: randomly initialized, not a prediction demo')
