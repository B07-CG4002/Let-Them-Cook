"""Controlled AI-only experiments. Lab runs deliberately refuse production config."""
import hashlib
import json
import platform
import random
import shutil
from pathlib import Path
import numpy as np
from src.hand_ai_flex3.core import CHANNELS,LABELS,CONFIG
from src.hand_ai_lab.data import sha,write,load_capture,load_annotations
from src.hand_ai_lab.replay import ResearchWindow
from src.hand_ai_lab.metrics import classify,training_label

FEATURE_SETS={'all12':list(range(12)),'imu_flex2':list(range(11)),'imu9':list(range(9))}

def prepare_continuous(manifest,device,seconds,stride,out):
    if device not in (1,2) or not np.isfinite(stride) or stride<=0:raise ValueError('Invalid hand/stride')
    out=Path(out)
    if out.exists():raise FileExistsError(out)
    source=json.loads(Path(manifest).read_text(encoding='utf-8-sig'))
    if not isinstance(source,list) or not source:raise ValueError('Collection manifest must list capture/annotations pairs')
    windows=[];labels=[];metadata=[];groups={};capture_ids=set();fingerprints=set()
    for pair in source:
        capture=Path(pair['capture']);ann=Path(pair['annotations'])
        # Relative paths in collection manifests are relative to that manifest, never cwd.
        if not capture.is_absolute():capture=Path(manifest).resolve().parent/capture
        if not ann.is_absolute():ann=Path(manifest).resolve().parent/ann
        meta,samples=load_capture(capture);a=load_annotations(ann,capture,meta,samples)
        if a is None or meta['split'] not in ('train','validation','test'):raise ValueError('Explicit train/validation/test capture required')
        group=(meta['person'],meta['session'])
        if group in groups and groups[group]!=meta['split']:raise ValueError('Session leakage across splits')
        groups[group]=meta['split'];capture_hash=sha(capture)
        if capture_hash in capture_ids:raise ValueError('Capture duplicated in collection')
        capture_ids.add(capture_hash);window=ResearchWindow(device,seconds);history=[];due=-float('inf')
        for s in samples:
            if s['device_id']!=device:continue
            old=window.generation;added=window.add(s,s['elapsed_s'])
            if window.generation!=old:history=[];due=-float('inf')
            if not added:continue
            history.append((window.elapsed,s['elapsed_s']));history=history[-512:]
            job=window.job(s['elapsed_s'])
            if job is None or window.elapsed+1e-9<due:continue
            due=window.elapsed+stride
            start=float(np.interp(window.elapsed-seconds,[v[0] for v in history],[v[1] for v in history]))
            seg=training_label(a['segments'],device,start,s['elapsed_s'])
            if not seg:continue
            value=job.window.astype(np.float32);finger=hashlib.sha256(value.tobytes()).hexdigest()
            if finger in fingerprints:raise ValueError('Duplicate resampled window; review recordings before training')
            fingerprints.add(finger);windows.append(value);labels.append(LABELS.index(seg['gesture']))
            metadata.append(dict(person=meta['person'],session=meta['session'],split=meta['split'],device_id=device,
                gesture=seg['gesture'],source=str(capture.resolve()),capture_sha256=capture_hash,
                annotations_sha256=sha(ann),start_s=start,end_s=s['elapsed_s'],input_sha256=finger))
    if not windows:raise ValueError('No fully annotated pure windows; review segment duration and source continuity')
    validate_arrays(np.array(windows),np.array(labels),metadata)
    out.mkdir(parents=True);np.save(out/'X.npy',np.array(windows,dtype=np.float32));np.save(out/'y.npy',np.array(labels,dtype=np.int64))
    write(out/'metadata.json',metadata);write(out/'lab_data.json',dict(format=1,device_id=device,window_seconds=seconds,
        stride_s=stride,features=CHANNELS,class_names=LABELS,collection_manifest_sha256=sha(manifest),
        sha256={name:sha(out/name) for name in ('X.npy','y.npy','metadata.json')}))
    return dict(samples=len(labels),counts=counts(metadata),group_split_enforced=True)

def counts(meta):
    return {label:{split:sum(m['gesture']==label and m['split']==split for m in meta) for split in ('train','validation','test')} for label in LABELS}

def validate_arrays(x,y,meta):
    if x.shape!=(len(y),50,12) or len(meta)!=len(y) or not np.isfinite(x).all() or y.dtype.kind not in 'iu' or y.ndim!=1:raise ValueError('Invalid dataset arrays')
    groups={};fingers=set();hands=set()
    for value,label,m in zip(x,y,meta):
        if not 0<=int(label)<7 or LABELS[int(label)]!=m['gesture']:raise ValueError('Label mismatch')
        if type(m['device_id']) is not int or m['device_id'] not in (1,2):raise ValueError('Invalid dataset hand')
        hands.add(m['device_id']);group=(m['person'],m['session'])
        if m['split'] not in ('train','validation','test'):raise ValueError('Invalid dataset split')
        if group in groups and groups[group]!=m['split']:raise ValueError('Session leakage')
        groups[group]=m['split'];finger=hashlib.sha256(value.astype(np.float32).tobytes()).hexdigest()
        if finger!=m['input_sha256']:raise ValueError('Prepared input hash changed')
        if finger in fingers:raise ValueError('Duplicate model input')
        fingers.add(finger)
    if len(hands)!=1:raise ValueError('Prepare one hand at a time')
    if any(not n for row in counts(meta).values() for n in row.values()):raise ValueError('Each class needs at least one sample in each split')

def load_dataset(folder):
    folder=Path(folder)
    if (folder/'lab_data.json').exists():
        contract=json.loads((folder/'lab_data.json').read_text())
        for name,expected in contract['sha256'].items():
            if name not in ('X.npy','y.npy','metadata.json') or sha(folder/name)!=expected:raise ValueError('Lab dataset hash changed')
        if set(contract['sha256'])!={'X.npy','y.npy','metadata.json'} or contract['features']!=CHANNELS or contract['class_names']!=LABELS:raise ValueError('Lab dataset contract differs')
        x=np.load(folder/'X.npy',allow_pickle=False);y=np.load(folder/'y.npy',allow_pickle=False)
        meta=json.loads((folder/'metadata.json').read_text());seconds=contract['window_seconds']
        names=('X.npy','y.npy','metadata.json','lab_data.json')
    else:
        from src.hand_ai_flex3.pipeline import load_data
        x,y,frame=load_data(folder);meta=frame.to_dict('records');seconds=2.
        names=('X.npy','y.npy','metadata.csv','config.json')
    # Normalize pandas-derived scalar identities to plain JSON values.
    for m in meta:
        v=m['device_id']
        if isinstance(v,bool) or not isinstance(v,(int,float,np.integer,np.floating)) or not np.isfinite(v) or int(v)!=v:raise ValueError('Invalid metadata device identity')
        m['device_id']=int(v)
    validate_arrays(x,y,meta)
    return x,y,meta,dict(window_seconds=seconds,sha256={n:sha(folder/n) for n in names},folder=str(folder.resolve()))

def fit_normalizer(x):
    mean=x.mean((0,1),dtype=np.float64).astype(np.float32);std=x.std((0,1),dtype=np.float64).astype(np.float32)
    return mean,np.where(std<1e-6,1.,std).astype(np.float32)

def train(data,out,feature_set='all12',seeds=(42,73,101),epochs=30,noise_std=0.):
    import torch
    from src.model import GestureCNN
    if feature_set not in FEATURE_SETS or epochs<1 or not np.isfinite(noise_std) or noise_std<0:raise ValueError('Invalid training parameters')
    if not seeds or len(set(seeds))!=len(seeds) or any(type(s) is not int or not 0<=s<2**32 for s in seeds):raise ValueError('Need unique uint32 seeds')
    out=Path(out)
    if out.exists():raise FileExistsError(out)
    x,y,meta,source=load_dataset(data);ids={s:np.array([i for i,m in enumerate(meta) if m['split']==s]) for s in ('train','validation','test')}
    mean,std=fit_normalizer(x[ids['train']]);selected=FEATURE_SETS[feature_set];mask=np.zeros(12,dtype=np.float32);mask[selected]=1
    normalized=((x-mean)/std)*mask
    out.mkdir(parents=True);write(out/'status.json',dict(status='started',test_evaluated=False))
    summaries=[]
    try:
        for seed in seeds:
            random.seed(seed);np.random.seed(seed);torch.manual_seed(seed);torch.set_num_threads(1)
            torch.use_deterministic_algorithms(True)
            tx=torch.from_numpy(normalized.astype(np.float32)).permute(0,2,1);ty=torch.from_numpy(y.astype(np.int64))
            model=GestureCNN(num_classes=7,num_features=12);model(torch.zeros(1,12,50))
            generator=torch.Generator().manual_seed(seed)
            loader=torch.utils.data.DataLoader(torch.utils.data.TensorDataset(tx[ids['train']],ty[ids['train']]),batch_size=32,shuffle=True,generator=generator)
            optimizer=torch.optim.Adam(model.parameters(),lr=.001);loss_fn=torch.nn.CrossEntropyLoss();best=None;best_key=(-1.,-float('inf'));history=[]
            for epoch in range(epochs):
                model.train();total=0.
                for batch,target in loader:
                    if noise_std:batch=batch+torch.randn_like(batch)*noise_std*torch.from_numpy(mask)[None,:,None]
                    optimizer.zero_grad();loss=loss_fn(model(batch),target);loss.backward();optimizer.step();total+=float(loss.detach())*len(target)
                model.eval()
                with torch.inference_mode():
                    logits=model(tx[ids['validation']]);val_loss=float(loss_fn(logits,ty[ids['validation']]));pred=logits.argmax(1).numpy()
                metric=classify([LABELS[int(v)] for v in y[ids['validation']]],[LABELS[int(v)] for v in pred]);f1=metric['macro_f1_present_classes']
                row=dict(epoch=epoch+1,train_loss=total/len(ids['train']),validation_loss=val_loss,validation_macro_f1=f1,validation_accuracy=metric['accuracy'])
                if not all(np.isfinite(v) for v in row.values()):raise ValueError('Nonfinite training result')
                history.append(row)
                if (f1,-val_loss)>best_key:best_key=(f1,-val_loss);best={k:v.detach().cpu().clone() for k,v in model.state_dict().items()};best_epoch=epoch+1
                print(dict(seed=seed,feature_set=feature_set,**row),flush=True)
            run=out/f'seed_{seed}';run.mkdir();torch.save(best,run/'model.pth');np.savez(run/'normalization.npz',mean=mean,std=std)
            contract=dict(version='ai_lab_experiment_v1',class_names=LABELS,feature_columns=CHANNELS,feature_set=feature_set,
                active_indices=selected,window_seconds=source['window_seconds'],time_positions=50,device_id=meta[0]['device_id'],
                seed=seed,noise_std=noise_std,epochs=epochs,best_epoch=best_epoch,history=history,dataset=source,
                session_groups=sorted({m['person']+'/'+m['session'] for m in meta}),test_evaluated=False,
                environment=dict(python=platform.python_version(),torch=torch.__version__,numpy=np.__version__,device='cpu'),
                model_source_sha256=sha(Path(__import__('src.model',fromlist=['__file__']).__file__)),
                payload_sha256={n:sha(run/n) for n in ('model.pth','normalization.npz')},
                deployable=False,reason='Lab contract includes duration and ablation mask; existing production Predictor/export must refuse it.')
            write(run/'lab.json',contract);summaries.append(dict(seed=seed,run=str(run.resolve()),validation_macro_f1=best_key[0],best_epoch=best_epoch))
        write(out/'comparison.json',dict(feature_set=feature_set,runs=summaries,test_evaluated=False,
            mean_validation_macro_f1=float(np.mean([s['validation_macro_f1'] for s in summaries])),std_validation_macro_f1=float(np.std([s['validation_macro_f1'] for s in summaries]))))
        (out/'status.json').write_text(json.dumps(dict(status='finished',test_evaluated=False))+'\n')
    except BaseException as e:
        (out/'status.json').write_text(json.dumps(dict(status='failed',error_type=type(e).__name__,message=str(e)))+'\n');raise
    return summaries

class LabPredictor:
    def __init__(self,run):
        import torch
        from src.model import GestureCNN
        self.torch=torch;run=Path(run);c=json.loads((run/'lab.json').read_text());self.contract=c
        if c['version']!='ai_lab_experiment_v1' or c['class_names']!=LABELS or c['feature_columns']!=CHANNELS or c['active_indices']!=FEATURE_SETS[c['feature_set']]:raise ValueError('Invalid lab model contract')
        if c['window_seconds'] not in (.5,1.,1.5,2.):raise ValueError('Invalid model duration')
        for name in ('model.pth','normalization.npz'):
            if sha(run/name)!=c['payload_sha256'][name]:raise ValueError('Lab model payload hash differs')
        self.device_id=c['device_id'];self.mask=np.zeros(12,dtype=np.float32);self.mask[c['active_indices']]=1
        with np.load(run/'normalization.npz',allow_pickle=False) as z:self.mean,self.std=z['mean'],z['std']
        if self.mean.shape!=(12,) or self.std.shape!=(12,) or not np.isfinite(self.mean).all() or not np.isfinite(self.std).all() or np.any(self.std<=0):raise ValueError('Invalid normalizer')
        self.model=GestureCNN(num_classes=7,num_features=12);self.model(torch.zeros(1,12,50))
        self.model.load_state_dict(torch.load(run/'model.pth',map_location='cpu',weights_only=True));self.model.eval()
    def probabilities(self,x):
        x=np.asarray(x,dtype=np.float32)
        if x.ndim==2:x=x[None]
        if x.ndim!=3 or x.shape[1:]!=(50,12) or not np.isfinite(x).all():raise ValueError('Expected finite N,50,12')
        tensor=self.torch.from_numpy((((x-self.mean)/self.std)*self.mask).astype(np.float32)).permute(0,2,1)
        with self.torch.inference_mode():return self.torch.softmax(self.model(tensor),dim=1).numpy()

def evaluate(run,data,split,out,allow_test=False):
    if split not in ('validation','test') or split=='test' and not allow_test:raise ValueError('Test evaluation needs --allow-test-use; freeze settings first')
    out=Path(out)
    if out.exists():raise FileExistsError(out)
    p=LabPredictor(run);x,y,meta,source=load_dataset(data)
    if source['sha256']!=p.contract['dataset']['sha256']:raise ValueError('Evaluation dataset differs from training provenance')
    ids=[i for i,m in enumerate(meta) if m['split']==split];probs=p.probabilities(x[ids]);pred=probs.argmax(1)
    report=dict(split=split,test_use_recorded=split=='test',metrics=classify([LABELS[int(y[i])] for i in ids],[LABELS[int(v)] for v in pred]),
        run_sha256=sha(Path(run)/'lab.json'),predictions=[dict(index=i,truth=LABELS[int(y[i])],probabilities=q.tolist()) for i,q in zip(ids,probs)])
    write(out,report);return report

def export_native(run,out):
    """Explicitly promote only full-feature/two-second runs on native prepared data."""
    p=LabPredictor(run);c=p.contract;out=Path(out)
    if out.exists():raise FileExistsError(out)
    if c['feature_set']!='all12' or c['window_seconds']!=2.:
        raise ValueError('Ablation/short-window models need a new explicit deployment contract; no silent promotion')
    data=Path(c['dataset']['folder'])
    if (data/'lab_data.json').exists():raise ValueError('Continuous lab dataset uses a different manifest; use native prepared recordings for this compatibility export')
    x,y,meta,source=load_dataset(data)
    if source['sha256']!=c['dataset']['sha256']:raise ValueError('Dataset changed since training')
    out.mkdir(parents=True)
    for name in ('model.pth','normalization.npz'):shutil.copyfile(Path(run)/name,out/name)
    write(out/'config.json',CONFIG);write(out/'class_names.json',LABELS)
    shutil.copyfile(data/'metadata.csv',out/'split_manifest.csv')
    write(out/'training.json',dict(seed=c['seed'],epochs=c['epochs'],optimizer='Adam',learning_rate=.001,
        device_id=c['device_id'],data_dir=str(data.resolve()),dataset_sha256=source['sha256'],
        history=c['history'],selection='validation macro F1, then validation loss',
        lab_origin_sha256=sha(Path(run)/'lab.json'),test_evaluated=False))
    return dict(status='exported_native_contract',run=str(out.resolve()),board_validated=False)
