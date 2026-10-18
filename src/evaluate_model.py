"""Evaluate the saved test split, or a separate explicitly supplied holdout dataset."""
import hashlib
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score,classification_report,confusion_matrix
from src.ai_common import CLASS_NAMES,read_config
from src.inference import Predictor

def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--run-dir',required=True)
    p.add_argument('--holdout-dir'); p.add_argument('--output',required=True); a=p.parse_args()
    run=Path(a.run_dir); output=Path(a.output)
    if output.exists(): raise FileExistsError(output)
    training=json.loads((run/'training.json').read_text())
    data=Path(a.holdout_dir or training['data_dir']); read_config(data)
    X=np.load(data/'X.npy'); y=np.load(data/'y.npy'); meta=pd.read_csv(data/'metadata.csv')
    if a.holdout_dir:
        saved=pd.read_csv(run/'split_manifest.csv')
        old=set(zip(saved.person,saved.gesture,saved.trial)); new=set(zip(meta.person,meta.gesture,meta.trial))
        if old & new: raise ValueError('Holdout shares recording identities with development dataset')
        ids=np.arange(len(y))
    else:
        for name,digest in training['dataset_sha256'].items():
            if hashlib.sha256((data/name).read_bytes()).hexdigest()!=digest: raise ValueError('Dataset changed since training')
        saved=pd.read_csv(run/'split_manifest.csv')
        if len(saved)!=len(meta) or not saved[['person','gesture','trial','source']].equals(meta[['person','gesture','trial','source']]):
            raise ValueError('Dataset ordering/content identity no longer matches training manifest')
        with np.load(run/'splits.npz') as z: ids=z['test']
    predictor=Predictor(run); logits=predictor.logits(X[ids]); pred=logits.argmax(1)
    report=dict(kind='extra_holdout' if a.holdout_dir else 'saved_test_split',samples=len(ids),
        accuracy=float(accuracy_score(y[ids],pred)),class_names=CLASS_NAMES,
        confusion_matrix=confusion_matrix(y[ids],pred,labels=range(8)).tolist(),
        classification_report=classification_report(y[ids],pred,labels=range(8),target_names=CLASS_NAMES,zero_division=0,output_dict=True))
    output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(classification_report(y[ids],pred,labels=range(8),target_names=CLASS_NAMES,zero_division=0))
    print('Rows=true, columns=predicted:',CLASS_NAMES); print(np.array(report['confusion_matrix']))
    print('Accuracy:',f"{report['accuracy']:.2%}",'Saved',output)
if __name__=='__main__': main()
