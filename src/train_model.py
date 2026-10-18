"""Train eight classes using fixed, stratified whole-recording splits."""
import hashlib
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import DataLoader,TensorDataset
from src.model import GestureCNN
from src.ai_common import CLASS_NAMES,CONFIG,read_config

def split_indices(y,seed=42):
    # Largest-remainder rounding per class: 18 recordings -> 13/2/3.
    rng=np.random.default_rng(seed); parts=[[],[],[]]
    for c in range(8):
        ids=rng.permutation(np.flatnonzero(y==c)); n=len(ids)
        raw=n*np.array([.75,.10,.15]); counts=np.floor(raw).astype(int)
        for j in np.argsort(-(raw-counts),kind='stable')[:n-counts.sum()]: counts[j]+=1
        if np.any(counts==0): raise ValueError('Too few recordings for all three splits')
        offset=0
        for j,count in enumerate(counts): parts[j].extend(ids[offset:offset+count].tolist()); offset+=count
    return [np.array(sorted(p),dtype=int) for p in parts]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',required=True); p.add_argument('--run-dir',required=True)
    p.add_argument('--epochs',type=int,default=30); p.add_argument('--seed',type=int,default=42)
    a=p.parse_args(); data=Path(a.data_dir); run=Path(a.run_dir); read_config(data)
    if a.epochs<1: raise ValueError('epochs must be positive')
    if run.exists(): raise FileExistsError(f'Choose a NEW run folder: {run}')
    X=np.load(data/'X.npy'); y=np.load(data/'y.npy'); meta=pd.read_csv(data/'metadata.csv')
    if X.shape!=(len(y),50,22) or len(meta)!=len(y) or not np.isfinite(X).all(): raise ValueError('Invalid dataset')
    if set(y.tolist())!=set(range(8)): raise ValueError('Expected labels 0..7')
    if meta.duplicated(['person','gesture','trial']).any(): raise ValueError('Duplicate original recordings')
    if any(CLASS_NAMES[int(label)]!=gesture for label,gesture in zip(y,meta.gesture)): raise ValueError('Metadata/labels mismatch')
    train,val,test=split_indices(y,a.seed)
    torch.manual_seed(a.seed); np.random.seed(a.seed)
    model=GestureCNN(num_classes=8,num_features=22)
    model(torch.zeros(1,22,50))
    tx=torch.from_numpy(X.astype(np.float32)).permute(0,2,1); ty=torch.from_numpy(y.astype(np.int64))
    loader=DataLoader(TensorDataset(tx[train],ty[train]),batch_size=32,shuffle=True)
    optimizer=torch.optim.Adam(model.parameters(),lr=.001); criterion=nn.CrossEntropyLoss()
    best=None; score=(-1,float('-inf')); history=[]
    for epoch in range(a.epochs):
        model.train(); total=0
        for inputs,labels in loader:
            optimizer.zero_grad(); loss=criterion(model(inputs),labels); loss.backward(); optimizer.step()
            total+=float(loss.detach())*len(labels)
        model.eval()
        with torch.no_grad():
            logits=model(tx[val]); vl=float(criterion(logits,ty[val])); va=float((logits.argmax(1)==ty[val]).float().mean())
        row=dict(epoch=epoch+1,train_loss=total/len(train),val_loss=vl,val_accuracy=va); history.append(row); print(row)
        if (va,-vl)>score:
            score=(va,-vl); best={k:v.detach().cpu().clone() for k,v in model.state_dict().items()}
    run.mkdir(parents=True)
    torch.save(best,run/'model.pth')
    np.savez(run/'splits.npz',train=train,validation=val,test=test)
    manifest=meta.copy(); manifest['split']=''
    for name,ids in [('train',train),('validation',val),('test',test)]:
        manifest.loc[ids,'split']=name; print(name,len(ids),f'{len(ids)/len(y):.1%}')
    manifest.to_csv(run/'split_manifest.csv',index=False)
    (run/'config.json').write_text(json.dumps(CONFIG,indent=2),encoding='utf-8')
    (run/'class_names.json').write_text(json.dumps(CLASS_NAMES,indent=2),encoding='utf-8')
    (run/'training.json').write_text(json.dumps(dict(seed=a.seed,epochs=a.epochs,learning_rate=.001,
        optimizer='Adam',loss='CrossEntropyLoss',data_dir=str(data.resolve()),
        dataset_sha256={name:hashlib.sha256((data/name).read_bytes()).hexdigest() for name in ('X.npy','y.npy','metadata.csv')},history=history),indent=2),encoding='utf-8')
    print('Saved best validation checkpoint to',run,'; run evaluation separately.')
if __name__=='__main__': main()
