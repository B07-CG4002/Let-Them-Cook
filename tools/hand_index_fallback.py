"""Temporary RIGHT-only model: mask faulty index, fold mask into native weights."""
import argparse
import json
from pathlib import Path
import shutil
import numpy as np
from src.hand_ai_lab import training as lab
from src.hand_ai_lab.data import write,sha
from src.hand_ai_flex3.core import CONFIG,LABELS

PROFILE='right_no_index'
INDICES=[i for i in range(12) if i!=9]


def register():lab.FEATURE_SETS[PROFILE]=INDICES


def fold_index(state):
    result={k:v.clone() for k,v in state.items()}
    if result['features.0.weight'].shape!=(32,12,3):raise ValueError('Unexpected architecture')
    result['features.0.weight'][:,9,:]=0
    return result


def export(run,out):
    register();run=Path(run);out=Path(out)
    if out.exists():raise FileExistsError(out)
    p=lab.LabPredictor(run);c=p.contract
    if c['device_id']!=2 or c['feature_set']!=PROFILE or c['window_seconds']!=2.:
        raise ValueError('Need a RIGHT no-index, two-second model')
    data=Path(c['dataset']['folder'])
    if (data/'lab_data.json').exists():raise ValueError('This native export requires native trial preparation')
    x,y,meta,source=lab.load_dataset(data)
    if source['sha256']!=c['dataset']['sha256']:raise ValueError('Dataset changed')
    state=fold_index(p.model.state_dict())
    # Prove the folded native input path agrees with masked validation predictions,
    # including artificially changed index values. No test examples are evaluated.
    ids=[i for i,m in enumerate(meta) if m['split']=='validation']
    probe=x[ids].copy();probe[:,:,9]=np.linspace(0,1,len(probe))[:,None]
    expected=p.probabilities(probe)
    p.model.load_state_dict(state);p.mask[:]=1
    actual=p.probabilities(probe)
    if not np.allclose(expected,actual,atol=1e-6,rtol=1e-5):raise ValueError('Folded-mask validation disagreement')
    out.mkdir(parents=True)
    p.torch.save(state,out/'model.pth')
    shutil.copyfile(run/'normalization.npz',out/'normalization.npz')
    shutil.copyfile(data/'metadata.csv',out/'split_manifest.csv')
    write(out/'config.json',CONFIG);write(out/'class_names.json',LABELS)
    write(out/'training.json',dict(seed=c['seed'],epochs=c['epochs'],device_id=2,
        data_dir=str(data.resolve()),dataset_sha256=source['sha256'],history=c['history'],
        optimizer='Adam',learning_rate=.001,selection='validation macro F1 then loss',
        feature_exclusion=dict(channel='flex1',index=9,gpio=34,reason='temporary faulty RIGHT index connection',
                               implementation='zero input channel during training; zero conv1 weights in exported model'),
        lab_origin_sha256=sha(run/'lab.json'),validation_fold_max_error=float(np.max(np.abs(expected-actual))),
        test_evaluated=False,hardware_validated=False))
    return dict(status='native_right_index_ignored',run=str(out.resolve()),test_evaluated=False)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    t=sub.add_parser('train');t.add_argument('--data',required=True);t.add_argument('--out',required=True)
    t.add_argument('--epochs',type=int,default=30);t.add_argument('--seeds',type=int,nargs='+',default=[42,73,101])
    e=sub.add_parser('export');e.add_argument('--run',required=True);e.add_argument('--out',required=True)
    a=p.parse_args(argv);register()
    if a.command=='train':
        _,_,meta,_=lab.load_dataset(a.data)
        if {m['device_id'] for m in meta}!={2}:raise ValueError('This fallback is RIGHT-only')
        result=lab.train(a.data,a.out,PROFILE,a.seeds,a.epochs)
    else:result=export(a.run,a.out)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
