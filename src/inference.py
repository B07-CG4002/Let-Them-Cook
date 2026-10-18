"""Reusable CPU PyTorch inference; receives already-preprocessed 50x22 arrays."""
from pathlib import Path
import numpy as np
import torch
from src.model import GestureCNN
from src.ai_common import CLASS_NAMES,read_config
class Predictor:
    def __init__(self,run_dir):
        self.run=Path(run_dir); read_config(self.run)
        self.model=GestureCNN(num_classes=8,num_features=22)
        self.model(torch.zeros(1,22,50))
        self.model.load_state_dict(torch.load(self.run/'model.pth',map_location='cpu',weights_only=True))
        self.model.eval()
    def logits(self,windows):
        values=np.asarray(windows,dtype=np.float32)
        if values.ndim==2: values=values[None]
        if values.shape[1:]!=(50,22) or not np.isfinite(values).all(): raise ValueError('Expected finite (N,50,22)')
        with torch.no_grad(): return self.model(torch.from_numpy(values).permute(0,2,1)).numpy()
    def predict_window(self,window):
        scores=self.logits(window)[0]; probs=np.exp(scores-scores.max()); probs/=probs.sum(); i=int(probs.argmax())
        return dict(gesture=CLASS_NAMES[i],confidence=float(probs[i]),probabilities=probs.tolist())
