"""Prepare, train and evaluate the seven-class single-hand dataset."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
from src.hand_ai.core import (CONFIG, CHANNELS, LABELS, SPLITS, digest, write_json,
                              resample, validate_ratios, fit_normalizer, check_config)


def prepare(a):
    root, out = Path(a.input), Path(a.out)
    if out.exists():
        raise FileExistsError(out)
    records, windows, labels, seen, fingerprints = [], [], [], set(), set()
    for path in sorted(root.rglob('*.csv')):
        sidecar = path.with_suffix('.json')
        meta = json.loads(sidecar.read_text(encoding='utf-8'))
        if meta['device_id'] != a.device:
            raise ValueError(f'Wrong hand in {path}')
        sha = digest(path)
        if sha != meta['csv_sha256']:
            raise ValueError(f'CSV changed after recording: {path}')
        key = tuple(meta[k] for k in ('person','session','gesture','trial'))
        if key in seen:
            raise ValueError(f'Duplicate recording identity: {key}')
        seen.add(key)
        frame = pd.read_csv(path)
        required = ['elapsed_s','device_id','boot_id','seq','uptime_ms',*CHANNELS]
        if any(c not in frame for c in required):
            raise ValueError(f'Missing columns: {path}')
        if len(frame) < 85 or not np.isfinite(frame[required].to_numpy(dtype=float)).all():
            raise ValueError(f'Too few samples or nonfinite values: {path}')
        for column in ('device_id','boot_id','seq','uptime_ms'):
            v = frame[column].to_numpy(dtype=float)
            if np.any(v != np.floor(v)) or np.any(v < 0) or np.any(v > 2**32-1):
                raise ValueError(f'Invalid integer {column}: {path}')
        if not (frame.device_id == a.device).all() or frame.boot_id.nunique() != 1:
            raise ValueError(f'Identity changed: {path}')
        seq = frame.seq.to_numpy(dtype=np.int64)
        if np.any((np.diff(seq) % 2**32) != 1):
            raise ValueError(f'Sequence gap: {path}')
        uptime = frame.uptime_ms.to_numpy(dtype=np.int64)
        dt = np.diff(uptime) % 2**32
        if np.any(dt <= 0) or np.any(dt > 100):
            raise ValueError(f'Source timestamp gap: {path}')
        t = frame.elapsed_s.to_numpy(dtype=float)
        source_t = np.r_[0., np.cumsum(dt) / 1000.]
        if not np.allclose(t, source_t, atol=1e-6) or not 1.85 <= t[-1] <= 2.1:
            raise ValueError(f'Invalid two-second source timing: {path}')
        values = frame[CHANNELS].to_numpy(dtype=float)
        window = resample(t, values)
        import hashlib
        fingerprint = hashlib.sha256(window.tobytes()).hexdigest()
        if fingerprint in fingerprints:
            raise ValueError(f'Identical model input duplicated: {path}')
        fingerprints.add(fingerprint)
        if meta['gesture'] not in LABELS or meta['split'] not in SPLITS:
            raise ValueError(f'Invalid label or split: {path}')
        windows.append(window)
        labels.append(LABELS.index(meta['gesture']))
        records.append({**{k:meta[k] for k in ('person','session','gesture','trial','split','device_id')},
                        'source':str(path.resolve()), 'sha256':sha, 'input_sha256':fingerprint})
    counts = validate_ratios(records)
    out.mkdir(parents=True)
    np.save(out/'X.npy', np.asarray(windows, dtype=np.float32))
    np.save(out/'y.npy', np.asarray(labels, dtype=np.int64))
    pd.DataFrame(records).to_csv(out/'metadata.csv', index=False)
    write_json(out/'config.json', CONFIG)
    print(json.dumps(counts, indent=2))
    print('Saved', out, 'shape:', np.asarray(windows).shape)


def load_data(folder):
    folder = Path(folder)
    check_config(folder/'config.json')
    x, y = np.load(folder/'X.npy'), np.load(folder/'y.npy')
    meta = pd.read_csv(folder/'metadata.csv', dtype={'person':str,'session':str})
    if x.shape != (len(y),50,11) or len(meta) != len(y) or not np.isfinite(x).all():
        raise ValueError('Invalid dataset dimensions/values')
    if y.ndim != 1 or y.dtype.kind not in 'iu' or set(y.tolist()) != set(range(7)):
        raise ValueError('Expected integer labels 0..6')
    if any(LABELS[int(v)] != g for v,g in zip(y, meta.gesture)):
        raise ValueError('Label/metadata mismatch')
    if meta.device_id.nunique() != 1 or meta.duplicated(['person','session','gesture','trial']).any():
        raise ValueError('Mixed hands or duplicate identities')
    if meta.input_sha256.duplicated().any():
        raise ValueError('Duplicate model inputs')
    validate_ratios(meta.to_dict('records'))
    return x, y, meta


def train(a):
    import torch
    from torch import nn
    from torch.utils.data import DataLoader, TensorDataset
    from src.model import GestureCNN
    data, run = Path(a.data), Path(a.run)
    if run.exists():
        raise FileExistsError(run)
    if a.epochs < 1:
        raise ValueError('epochs must be positive')
    x, y, meta = load_data(data)
    ids = {s: np.flatnonzero(meta.split == s) for s in SPLITS}
    mean, std = fit_normalizer(x[ids['train']])
    torch.manual_seed(a.seed)
    torch.set_num_threads(1)
    tx = torch.from_numpy(((x-mean)/std).astype(np.float32)).permute(0,2,1)
    ty = torch.from_numpy(y.astype(np.int64))
    model = GestureCNN(num_classes=7, num_features=11)
    model(torch.zeros(1,11,50))
    loader = DataLoader(TensorDataset(tx[ids['train']],ty[ids['train']]), batch_size=32, shuffle=True)
    opt = torch.optim.Adam(model.parameters(), lr=.001)
    loss_fn = nn.CrossEntropyLoss()
    best, score, history = None, (-1.,float('-inf')), []
    for epoch in range(a.epochs):
        model.train()
        total = 0.
        for inputs, targets in loader:
            opt.zero_grad()
            loss = loss_fn(model(inputs), targets)
            loss.backward()
            opt.step()
            total += loss.item()*len(targets)
        model.eval()
        with torch.no_grad():
            output = model(tx[ids['validation']])
            vl = loss_fn(output,ty[ids['validation']]).item()
            va = (output.argmax(1) == ty[ids['validation']]).float().mean().item()
        row = dict(epoch=epoch+1,train_loss=total/len(ids['train']),val_loss=vl,val_accuracy=va)
        if not all(np.isfinite(list(row.values()))):
            raise ValueError('Nonfinite training metrics')
        history.append(row)
        print(row, flush=True)
        if (va,-vl) > score:
            score = (va,-vl)
            best = {k:v.detach().cpu().clone() for k,v in model.state_dict().items()}
    run.mkdir(parents=True)
    torch.save(best, run/'model.pth')
    np.savez(run/'normalization.npz', mean=mean, std=std)
    write_json(run/'config.json', CONFIG)
    write_json(run/'class_names.json', LABELS)
    meta.to_csv(run/'split_manifest.csv', index=False)
    write_json(run/'training.json', dict(seed=a.seed, epochs=a.epochs, optimizer='Adam', learning_rate=.001,
               device_id=int(meta.device_id.iloc[0]), data_dir=str(data.resolve()), history=history,
               dataset_sha256={n:digest(data/n) for n in ('X.npy','y.npy','metadata.csv','config.json')}))
    print('Saved best validation checkpoint:', run, '; test set was not evaluated.')


class Predictor:
    def __init__(self, run):
        import torch
        from src.model import GestureCNN
        self.torch = torch
        run = Path(run)
        check_config(run/'config.json')
        if json.loads((run/'class_names.json').read_text()) != LABELS:
            raise ValueError('Class order mismatch')
        self.device_id = json.loads((run/'training.json').read_text())['device_id']
        with np.load(run/'normalization.npz') as z:
            self.mean, self.std = z['mean'].copy(), z['std'].copy()
        if self.mean.shape != (11,) or self.std.shape != (11,) or not np.isfinite(self.mean).all() or not np.isfinite(self.std).all() or np.any(self.std <= 0):
            raise ValueError('Invalid normalization')
        self.model = GestureCNN(num_classes=7,num_features=11)
        self.model(torch.zeros(1,11,50))
        self.model.load_state_dict(torch.load(run/'model.pth',map_location='cpu',weights_only=True))
        self.model.eval()
    def probabilities(self, x):
        x = np.asarray(x,dtype=np.float32)
        if x.ndim == 2:
            x = x[None]
        if x.ndim != 3 or x.shape[1:] != (50,11) or not len(x) or not np.isfinite(x).all():
            raise ValueError('Expected finite inputs (N,50,11)')
        tensor = self.torch.from_numpy(((x-self.mean)/self.std).astype(np.float32)).permute(0,2,1)
        with self.torch.no_grad():
            return self.torch.softmax(self.model(tensor),dim=1).numpy()


def evaluate(a):
    from sklearn.metrics import classification_report, confusion_matrix, accuracy_score
    run, out = Path(a.run), Path(a.out)
    if out.exists():
        raise FileExistsError(out)
    training = json.loads((run/'training.json').read_text())
    data = Path(a.data or training['data_dir'])
    for n, sha in training['dataset_sha256'].items():
        if digest(data/n) != sha:
            raise ValueError(f'Dataset changed since training: {n}')
    x,y,meta = load_data(data)
    ids = np.flatnonzero(meta.split == a.split)
    predictor = Predictor(run)
    probs = predictor.probabilities(x[ids])
    pred = probs.argmax(1)
    report = dict(split=a.split,samples=len(ids),class_names=LABELS,
                  accuracy=float(accuracy_score(y[ids],pred)),
                  confusion_matrix=confusion_matrix(y[ids],pred,labels=range(7)).tolist(),
                  classification_report=classification_report(y[ids],pred,labels=range(7),target_names=LABELS,zero_division=0,output_dict=True),
                  predictions=[dict(source=meta.iloc[int(i)].source,true=LABELS[int(y[i])],
                                    predicted=LABELS[int(p)],confidence=float(q.max()),probabilities=q.tolist())
                               for i,p,q in zip(ids,pred,probs)])
    out.parent.mkdir(parents=True,exist_ok=True)
    write_json(out,report)
    print(classification_report(y[ids],pred,labels=range(7),target_names=LABELS,zero_division=0))
    print('Rows=true; columns=predicted:', LABELS)
    print(np.array(report['confusion_matrix']))
    print('Accuracy:', report['accuracy'], 'Report:',out)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command',required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--input',required=True); prep.add_argument('--out',required=True)
    prep.add_argument('--device',type=int,choices=(1,2),default=2)
    tr = sub.add_parser('train')
    tr.add_argument('--data',required=True); tr.add_argument('--run',required=True)
    tr.add_argument('--epochs',type=int,default=30); tr.add_argument('--seed',type=int,default=42)
    ev = sub.add_parser('evaluate')
    ev.add_argument('--run',required=True); ev.add_argument('--data')
    ev.add_argument('--split',choices=('validation','test'),required=True); ev.add_argument('--out',required=True)
    a = p.parse_args()
    {'prepare':prepare,'train':train,'evaluate':evaluate}[a.command](a)

if __name__ == '__main__':
    main()
