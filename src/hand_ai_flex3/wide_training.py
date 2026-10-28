"""Explicit v5 dataset/training path; separate from legacy v4 preparation."""
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import shutil

import numpy as np

from src.hand_ai_flex3.core import CONFIG, CHANNELS, LABELS, SPLITS, resample, validate_ratios, fit_normalizer
from src.hand_ai_flex3.wide_ble import RECORDING_CONTRACT, TRANSPORT
from tools.gyro_trial_audit import audit_trial

WIDE_CONFIG=dict(CONFIG,version='independent_hand_flex3_gyro_v5_v1',sensor_packet_version=5,
                 gyro_transport_scale=10,gyro_sensor_full_scale_dps=1000)
DATA_FILES=('X.npy','y.npy','metadata.csv','config.json')


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path,value):
    with Path(path).open('x',encoding='utf-8') as stream:
        json.dump(value,stream,indent=2,allow_nan=False);stream.write('\n')


def read_hand(root,device):
    """Read only that hand; validate raw bytes and explicit acquisition metadata."""
    root=Path(root)/('left' if device==1 else 'right')
    if device not in (1,2) or not root.is_dir():raise ValueError('Selected hand folder missing')
    records=[];windows=[];labels=[];identities=set();fingerprints=set();raw_hashes=set()
    for sidecar in root.rglob('*.json'):
        if sidecar.name!='session.json' and not sidecar.with_suffix('.csv').is_file():
            raise ValueError('Orphan/unknown recording sidecar: '+str(sidecar))
    for path in sorted(root.rglob('*.csv')):
        meta=json.loads(path.with_suffix('.json').read_text())
        marker_path=path.parent/'session.json';marker=json.loads(marker_path.read_text())
        if not isinstance(meta,dict) or not isinstance(marker,dict):raise ValueError('Recording metadata must be objects')
        if any(meta.get(k)!=marker.get(k) for k in ('person','session','split','device_id','recording_contract')):
            raise ValueError('Session/trial identity mismatch: '+str(path))
        if meta.get('recording_contract')!=RECORDING_CONTRACT:
            raise ValueError('Explicit v5 acquisition contract required: '+str(path))
        check=audit_trial(path,device,meta['session'],sensor_version=5)
        if (any(a['wire_rail_samples'] or a['near_wire_limit_samples'] for a in check['gyro'].values()) or
                any(check['other_channel_wire_rail_samples'].values())):
            raise ValueError('Gyro lacks reviewed sensor/wire headroom: '+str(path))
        if not isinstance(meta.get('person'),str) or not meta['person'] or not isinstance(meta.get('session'),str) or not meta['session']:
            raise ValueError('Missing person/session identity')
        if type(meta.get('trial')) is not int or meta['trial']<1:raise ValueError('Invalid trial identity')
        identity=tuple(meta[k] for k in ('person','session','gesture','trial'))
        if identity in identities or check['sha256'] in raw_hashes:raise ValueError('Duplicate raw recording')
        identities.add(identity);raw_hashes.add(check['sha256'])
        with path.open(newline='',encoding='utf-8-sig') as stream:rows=list(csv.DictReader(stream))
        value=resample([float(r['elapsed_s']) for r in rows],[[float(r[c]) for c in CHANNELS] for r in rows])
        fingerprint=hashlib.sha256(value.tobytes()).hexdigest()
        if fingerprint in fingerprints:raise ValueError('Duplicate resampled input')
        fingerprints.add(fingerprint);windows.append(value);labels.append(LABELS.index(meta['gesture']))
        records.append(dict(**{k:meta[k] for k in ('person','session','split','device_id','gesture','trial')},
            source=str(path.resolve()),sha256=check['sha256'],sidecar_sha256=sha(path.with_suffix('.json')),
            session_marker_sha256=sha(marker_path),input_sha256=fingerprint,
            acquisition_transport_json=json.dumps(meta['acquisition_transport'],sort_keys=True)))
    if not records:raise ValueError('No v5 trials for selected hand')
    return np.asarray(windows,dtype=np.float32),np.asarray(labels,dtype=np.int64),records


def progress(root):
    report={}
    for device,hand in ((1,'left'),(2,'right')):
        try:
            x,y,records=read_hand(root,device)
            counts={g:{s:sum(r['gesture']==g and r['split']==s for r in records) for s in SPLITS} for g in LABELS}
            try:validate_ratios(records);ready=True
            except ValueError:ready=False
            report[hand]=dict(status='ratio_complete' if ready else 'incomplete',valid_trials=len(y),counts=counts)
        except (OSError,ValueError,KeyError,TypeError) as error:
            report[hand]=dict(status='blocked',error=str(error))
    return report


def prepare(root,out):
    out=Path(out)
    if out.exists():raise FileExistsError(out)
    # Validate both hands fully before creating the prepared output.
    hands={d:read_hand(root,d) for d in (1,2)}
    for x,y,records in hands.values():validate_ratios(records)
    out.mkdir(parents=True)
    for device,(x,y,records) in hands.items():
        folder=out/('left' if device==1 else 'right');folder.mkdir()
        np.save(folder/'X.npy',x);np.save(folder/'y.npy',y)
        with (folder/'metadata.csv').open('x',newline='',encoding='utf-8') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(records[0]));writer.writeheader();writer.writerows(records)
        write(folder/'config.json',WIDE_CONFIG)
        write(folder/'dataset_manifest.json',dict(version=1,device_id=device,
              raw_root=str(Path(root).resolve()),sha256={n:sha(folder/n) for n in DATA_FILES},
              scope='v5 physical-unit trials; explicit split sessions; no normalization applied'))
    return dict(status='prepared',out=str(out.resolve()),trials={str(d):len(hands[d][1]) for d in hands})


def load_data(folder):
    folder=Path(folder);manifest=json.loads((folder/'dataset_manifest.json').read_text())
    if manifest.get('version')!=1 or manifest.get('device_id') not in (1,2):raise ValueError('Invalid prepared manifest')
    if manifest.get('sha256')!={n:sha(folder/n) for n in DATA_FILES}:raise ValueError('Prepared dataset hash changed')
    if json.loads((folder/'config.json').read_text())!=WIDE_CONFIG:raise ValueError('Wrong v5 model contract')
    x=np.load(folder/'X.npy',allow_pickle=False);y=np.load(folder/'y.npy',allow_pickle=False)
    with (folder/'metadata.csv').open(newline='',encoding='utf-8') as stream:records=list(csv.DictReader(stream))
    if x.shape!=(len(y),50,12) or not len(y) or len(records)!=len(y) or x.dtype!=np.float32 or y.dtype.kind not in 'iu' or not np.isfinite(x).all():
        raise ValueError('Invalid prepared arrays')
    fingerprints=set();identities=set()
    for value,label,row in zip(x,y,records):
        if not 0<=int(label)<7 or row['gesture']!=LABELS[int(label)] or int(row['device_id'])!=manifest['device_id']:
            raise ValueError('Prepared labels/hand mismatch')
        fingerprint=hashlib.sha256(value.tobytes()).hexdigest()
        if fingerprint!=row['input_sha256'] or fingerprint in fingerprints:raise ValueError('Prepared input duplicate or changed')
        fingerprints.add(fingerprint)
        identity=tuple(row[k] for k in ('person','session','gesture','trial'))
        if identity in identities:raise ValueError('Prepared duplicate identity')
        identities.add(identity)
        if json.loads(row['acquisition_transport_json'])!=TRANSPORT:raise ValueError('Prepared acquisition contract changed')
    validate_ratios(records)
    return x,y,records,manifest


def metrics(truth,predicted):
    matrix=np.zeros((7,7),dtype=np.int64)
    for t,p in zip(truth,predicted):matrix[int(t),int(p)]+=1
    tp=np.diag(matrix);den=matrix.sum(0)+matrix.sum(1)
    f1=np.divide(2*tp,den,out=np.zeros(7,dtype=float),where=den>0)
    return dict(samples=int(matrix.sum()),accuracy=float(tp.sum()/matrix.sum()) if matrix.sum() else 0.,
                macro_f1=float(f1.mean()),per_class_f1=dict(zip(LABELS,map(float,f1))),
                confusion_matrix=matrix.tolist(),class_names=LABELS)


class Predictor:
    def __init__(self,run):
        import torch
        from src.model import GestureCNN
        self.torch=torch;run=Path(run)
        if json.loads((run/'config.json').read_text())!=WIDE_CONFIG or json.loads((run/'class_names.json').read_text())!=LABELS:
            raise ValueError('Not a reviewed v5 hand model')
        self.training=json.loads((run/'training.json').read_text());self.device_id=self.training['device_id']
        if self.device_id not in (1,2):raise ValueError('Invalid model hand')
        for name,expected in self.training['model_payload_sha256'].items():
            if name not in ('model.pth','normalization.npz','config.json','class_names.json') or sha(run/name)!=expected:
                raise ValueError('Model payload changed')
        if set(self.training['model_payload_sha256'])!={'model.pth','normalization.npz','config.json','class_names.json'}:
            raise ValueError('Incomplete payload manifest')
        with np.load(run/'normalization.npz',allow_pickle=False) as z:self.mean,self.std=z['mean'].copy(),z['std'].copy()
        if self.mean.shape!=(12,) or self.std.shape!=(12,) or not np.isfinite(self.mean).all() or not np.isfinite(self.std).all() or np.any(self.std<=0):
            raise ValueError('Invalid normalization')
        self.model=GestureCNN(num_classes=7,num_features=12);self.model(torch.zeros(1,12,50))
        state=torch.load(run/'model.pth',map_location='cpu',weights_only=True)
        if self.device_id==2 and self.training.get('right_index_excluded'):
            if torch.count_nonzero(state['features.0.weight'][:,9,:]).item():raise ValueError('RIGHT index exclusion is not folded into weights')
        self.model.load_state_dict(state);self.model.eval()

    def probabilities(self,x):
        x=np.asarray(x,dtype=np.float32)
        if x.ndim==2:x=x[None]
        if x.ndim!=3 or x.shape[1:]!=(50,12) or not len(x) or not np.isfinite(x).all():raise ValueError('Expected finite N,50,12')
        tensor=self.torch.from_numpy(((x-self.mean)/self.std).astype(np.float32)).permute(0,2,1)
        with self.torch.inference_mode():return self.torch.softmax(self.model(tensor),dim=1).numpy()


def train(data,out,epochs=30,seeds=(42,73,101)):
    import torch
    from src.model import GestureCNN
    if epochs<1 or not seeds or len(set(seeds))!=len(seeds) or any(type(s) is not int or not 0<=s<2**32 for s in seeds):
        raise ValueError('Invalid epochs/seeds')
    data=Path(data);out=Path(out)
    if out.exists():raise FileExistsError(out)
    datasets={d:load_data(data/h) for d,h in ((1,'left'),(2,'right'))}
    out.mkdir(parents=True);write(out/'status.json',dict(status='started',test_evaluated=False))
    selection={}
    try:
        for device,hand in ((1,'left'),(2,'right')):
            x,y,records,manifest=datasets[device]
            if manifest['device_id']!=device:raise ValueError('Dataset hand mismatch')
            ids={s:np.array([i for i,r in enumerate(records) if r['split']==s],dtype=np.int64) for s in SPLITS}
            mean,std=fit_normalizer(x[ids['train']])
            # This excluded feature has no learned contribution. Identity scaling
            # also prevents a nearly constant faulty index from overflowing the
            # FPGA input encoder if its raw value later jumps across 0..1.
            if device==2:mean[9]=0.;std[9]=1.
            normalized=((x-mean)/std).astype(np.float32)
            if device==2:normalized[:,:,9]=0
            tx=torch.from_numpy(normalized).permute(0,2,1);ty=torch.from_numpy(y.astype(np.int64))
            candidates=[]
            for seed in seeds:
                random.seed(seed);np.random.seed(seed);torch.manual_seed(seed);torch.set_num_threads(1)
                model=GestureCNN(num_classes=7,num_features=12);model(torch.zeros(1,12,50))
                optimizer=torch.optim.Adam(model.parameters(),lr=.001);loss_fn=torch.nn.CrossEntropyLoss()
                loader=torch.utils.data.DataLoader(torch.utils.data.TensorDataset(tx[ids['train']],ty[ids['train']]),batch_size=32,shuffle=True)
                best=None;score=(-1.,float('-inf'));history=[];best_epoch=0
                for epoch in range(epochs):
                    model.train();total=0.
                    for batch,target in loader:
                        optimizer.zero_grad();loss=loss_fn(model(batch),target);loss.backward();optimizer.step()
                        total+=float(loss.detach())*len(target)
                    model.eval()
                    with torch.inference_mode():
                        logits=model(tx[ids['validation']]);vl=float(loss_fn(logits,ty[ids['validation']]))
                        met=metrics(y[ids['validation']],logits.argmax(1).numpy())
                    row=dict(epoch=epoch+1,train_loss=total/len(ids['train']),validation_loss=vl,
                             validation_macro_f1=met['macro_f1'],validation_accuracy=met['accuracy'])
                    if not all(np.isfinite(v) for v in row.values()):raise ValueError('Nonfinite training metrics')
                    history.append(row);print(dict(hand=hand,seed=seed,**row),flush=True)
                    if (met['macro_f1'],-vl)>score:
                        score=(met['macro_f1'],-vl);best={k:v.detach().cpu().clone() for k,v in model.state_dict().items()};best_epoch=epoch+1
                # Fold the RIGHT mask into weights for every downstream runtime.
                if device==2:best['features.0.weight'][:,9,:]=0
                run=out/'runs'/hand/f'seed_{seed}';run.mkdir(parents=True)
                torch.save(best,run/'model.pth');np.savez(run/'normalization.npz',mean=mean,std=std)
                write(run/'config.json',WIDE_CONFIG);write(run/'class_names.json',LABELS)
                shutil.copyfile(data/hand/'metadata.csv',run/'split_manifest.csv')
                write(run/'training.json',dict(seed=seed,epochs=epochs,best_epoch=best_epoch,history=history,
                    optimizer='Adam',learning_rate=.001,device_id=device,data_dir=str((data/hand).resolve()),
                    dataset_sha256={n:sha(data/hand/n) for n in DATA_FILES},selection='validation macro F1, then loss',
                    right_index_excluded=device==2,excluded_channel='flex1/GPIO34' if device==2 else None,
                    excluded_feature_normalization='identity (mean=0,std=1)' if device==2 else None,
                    model_payload_sha256={n:sha(run/n) for n in ('model.pth','normalization.npz','config.json','class_names.json')},
                    model_source_sha256=sha(Path(__import__('src.model',fromlist=['__file__']).__file__)),
                    provisional=True,test_evaluated=False,hardware_validated=False,created_utc=datetime.now(timezone.utc).isoformat()))
                predictor=Predictor(run);prob=predictor.probabilities(x[ids['validation']])
                folded=metrics(y[ids['validation']],prob.argmax(1))
                if abs(folded['macro_f1']-score[0])>1e-9:raise ValueError('Folded-model validation differs')
                if device==2:
                    probe=x[ids['validation']].copy();probe[:,:,9]=np.linspace(0,1,len(probe))[:,None]
                    if not np.allclose(prob,predictor.probabilities(probe),rtol=1e-6,atol=1e-7):raise ValueError('RIGHT index still affects predictions')
                write(run/'validation.json',folded)
                candidates.append(dict(seed=seed,run=str(run.resolve()),validation_macro_f1=score[0],validation_loss=-score[1]))
            chosen=max(candidates,key=lambda c:(c['validation_macro_f1'],-c['validation_loss'],-c['seed']))
            target=out/'selected'/hand;target.parent.mkdir(exist_ok=True);shutil.copytree(chosen['run'],target)
            selection[hand]=dict(selected_seed=chosen['seed'],candidates=candidates,
                                 model_sha256=sha(target/'model.pth'),normalization_sha256=sha(target/'normalization.npz'))
        write(out/'selected/selection.json',dict(version=1,selection=selection,test_evaluated=False,
              scope='provisional validation-selected models; live/board/game verification pending'))
        (out/'status.json').write_text(json.dumps(dict(status='finished',test_evaluated=False))+'\n')
    except BaseException as error:
        (out/'status.json').write_text(json.dumps(dict(status='failed',error_type=type(error).__name__,message=str(error)))+'\n');raise
    return dict(status='trained',selected=str((out/'selected').resolve()),selection=selection,test_evaluated=False)


def evaluate(runs,data,split,out,allow_test=False):
    if split not in ('validation','test') or split=='test' and not allow_test:raise ValueError('Test evaluation requires explicit --allow-test-use after freezing settings')
    out=Path(out)
    if out.exists():raise FileExistsError(out)
    result=dict(split=split,test_use_recorded=split=='test',models={})
    for device,hand in ((1,'left'),(2,'right')):
        predictor=Predictor(Path(runs)/hand);x,y,rows,manifest=load_data(Path(data)/hand)
        if predictor.device_id!=device or predictor.training['dataset_sha256']!=manifest['sha256']:raise ValueError('Model/dataset provenance differs')
        ids=[i for i,r in enumerate(rows) if r['split']==split];prob=predictor.probabilities(x[ids])
        result['models'][hand]=dict(metrics=metrics(y[ids],prob.argmax(1)),model_sha256=sha(Path(runs)/hand/'model.pth'),
            predictions=[dict(source=rows[i]['source'],truth=LABELS[int(y[i])],predicted=LABELS[int(p.argmax())],probabilities=p.tolist()) for i,p in zip(ids,prob)])
    out.parent.mkdir(parents=True,exist_ok=True);write(out,result);return result
