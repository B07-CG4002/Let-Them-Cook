"""Export selected eight-class model weights for NumPy/PYNQ inference."""
import argparse
import json
from pathlib import Path
import numpy as np
from src.inference import Predictor
from src.ai_common import CONFIG

def main():
    p=argparse.ArgumentParser(); p.add_argument('--run-dir',required=True); p.add_argument('--output',required=True); a=p.parse_args()
    output=Path(a.output)
    if output.exists(): raise FileExistsError(output)
    model=Predictor(a.run_dir).model
    arrays={}
    for name,module in [('conv1',model.features[0]),('conv2',model.features[3]),('fc1',model.classifier[1]),('fc2',model.classifier[3])]:
        for short,attribute in [('w','weight'),('b','bias')]: arrays[f'{name}_{short}']=getattr(module,attribute).detach().numpy()
    arrays['config_json']=np.array(json.dumps(CONFIG))
    output.parent.mkdir(parents=True,exist_ok=True); np.savez_compressed(output,**arrays); print('Saved',output)
if __name__=='__main__': main()
