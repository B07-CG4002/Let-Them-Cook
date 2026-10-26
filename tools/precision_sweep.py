"""CPU-only bit-width exploration for the repository's GestureCNN architecture.

Legacy paired NPZ: --weights file.npz --data inputs.npz
Final three-flex run: --run model_folder --data prepared_folder --split validation
Writes new reports only. Never imports PYNQ, programs an FPGA or changes a model.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import sys
import numpy as np

NAMES=('conv1_w','conv2_w','fc1_w','fc2_w','conv1_b','conv2_b','fc1_b','fc2_b')
OLD_CLASSES=['PICKUP','DROP','CHOP','WASH','THROW','FIREEXT','POURING','IDLE']
HAND_CLASSES=['PICKUP','DROP','CHOP','WASH','THROW','POURING','IDLE']
HAND_CHANNELS=['ax','ay','az','gx','gy','gz','yaw','pitch','roll','flex1','flex2','flex3']
STATE_KEYS=('features.0.weight','features.3.weight','classifier.1.weight','classifier.3.weight',
            'features.0.bias','features.3.bias','classifier.1.bias','classifier.3.bias')

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def read_json(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def write_json(path,value):Path(path).write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf-8')

def validate_parameters(p,features,classes):
    shapes=((32,features,3),(64,32,3),(64,704),(classes,64),(32,),(64,),(64,),(classes,))
    if set(p)!=set(NAMES):raise ValueError('Unexpected or missing model parameters')
    result={}
    for name,shape in zip(NAMES,shapes):
        if np.asarray(p[name]).dtype.kind not in 'fiu':raise ValueError('Parameters must be real numeric arrays: '+name)
        value=np.asarray(p[name],dtype=np.float32)
        if value.shape!=shape or not np.isfinite(value).all():raise ValueError('Invalid parameter: '+name)
        result[name]=value.copy()
    return result

def load_source(weights=None,run=None):
    if weights is not None:
        weights=Path(weights)
        with np.load(weights,allow_pickle=False) as z:
            cfg=json.loads(str(z['config_json']))
            if cfg.get('class_names')!=OLD_CLASSES or cfg.get('normalization')!='none' or cfg.get('window_seconds')!=2.0 or cfg.get('time_positions')!=50:
                raise ValueError('NPZ mode requires the existing unnormalized, paired eight-class contract')
            expected=[f'{hand}_{c}' for hand in ('left','right') for c in ('ax_g','ay_g','az_g','gx_deg_s','gy_deg_s','gz_deg_s','yaw_deg','pitch_deg','roll_deg','flex_1','flex_2')]
            if cfg.get('feature_columns')!=expected:raise ValueError('Wrong paired feature order')
            p=validate_parameters({n:z[n] for n in NAMES},22,8)
        return dict(parameters=p,features=22,classes=OLD_CLASSES,config=cfg,normalizer=None,
                    provenance={str(weights.resolve()):digest(weights)},mode='legacy_paired_regression')
    run=Path(run);cfg=read_json(run/'config.json');training=read_json(run/'training.json')
    if (cfg.get('version')!='independent_hand_flex3_v1' or cfg.get('feature_columns')!=HAND_CHANNELS or
        cfg.get('class_names')!=HAND_CLASSES or cfg.get('normalization')!='training-only per-feature mean/std after resampling' or
        cfg.get('time_positions')!=50 or cfg.get('window_seconds')!=2.0 or cfg.get('sensor_packet_version')!=4 or
        cfg.get('sensor_packet_bytes')!=40 or cfg.get('flex_gpio')!={'flex1':34,'flex2':35,'flex3':39}):
        raise ValueError('Run mode requires the final three-flex 12-feature/seven-class preprocessing contract')
    if read_json(run/'class_names.json')!=HAND_CLASSES:raise ValueError('Class mapping mismatch')
    if training.get('device_id') not in (1,2):raise ValueError('Invalid model hand identity')
    with np.load(run/'normalization.npz',allow_pickle=False) as z:
        mean,std=z['mean'].copy(),z['std'].copy()
    if mean.shape!=(12,) or std.shape!=(12,) or not np.isfinite(mean).all() or not np.isfinite(std).all() or np.any(std<=0):
        raise ValueError('Invalid normalization')
    import torch
    state=torch.load(run/'model.pth',map_location='cpu',weights_only=True)
    if set(state)!=set(STATE_KEYS):raise ValueError('Checkpoint architecture differs from GestureCNN')
    p=validate_parameters({n:state[k].detach().cpu().numpy() for n,k in zip(NAMES,STATE_KEYS)},12,7)
    files=[run/n for n in ('model.pth','config.json','class_names.json','normalization.npz','training.json')]
    return dict(parameters=p,features=12,classes=HAND_CLASSES,config=cfg,normalizer=(mean,std),
                training=training,provenance={str(f.resolve()):digest(f) for f in files},mode='three_flex_run')

def validate_inputs(x,y,features,classes):
    x=np.asarray(x);y=np.asarray(y)
    if x.dtype.kind not in 'fiu' or x.ndim!=3 or x.shape[1:]!=(50,features) or len(x)==0 or not np.isfinite(x).all():
        raise ValueError(f'Expected nonempty finite inputs (N,50,{features})')
    if y.shape!=(len(x),) or y.dtype.kind not in 'iu' or np.any(y<0) or np.any(y>=classes):raise ValueError('Invalid integer labels')
    return x.astype(np.float32),y.astype(np.int64)

def load_data(path,source,split):
    path=Path(path);features=source['features'];classes=len(source['classes'])
    if source['mode']=='legacy_paired_regression':
        if not path.is_file():raise ValueError('Legacy mode expects a board-test NPZ with inputs and labels')
        with np.load(path,allow_pickle=False) as z:x,y=validate_inputs(z['inputs'],z['labels'],features,classes)
        return x,y,x,dict(evaluation_scope='older paired regression vectors; not final-model accuracy',
                         calibration_scope='same regression vectors; diagnostic range profiling only',
                         data_sha256={str(path.resolve()):digest(path)},split='unassigned_legacy')
    if not path.is_dir():raise ValueError('Three-flex mode requires the prepared dataset directory, not an unlabelled NPZ')
    if read_json(path/'config.json')!=source['config']:raise ValueError('Dataset/model preprocessing contracts differ')
    required=('X.npy','y.npy','metadata.csv','config.json')
    expected=source['training'].get('dataset_sha256',{})
    hashes={name:digest(path/name) for name in required}
    if set(expected)!=set(required) or hashes!=expected:raise ValueError('Dataset does not match the model training provenance')
    x,y=validate_inputs(np.load(path/'X.npy',allow_pickle=False),np.load(path/'y.npy',allow_pickle=False),features,classes)
    with (path/'metadata.csv').open(newline='',encoding='utf-8-sig') as stream:rows=list(csv.DictReader(stream))
    if len(rows)!=len(x):raise ValueError('Metadata length mismatch')
    owners={};identities=set()
    for row,label in zip(rows,y):
        if row['split'] not in ('train','validation','test') or row['gesture']!=source['classes'][label] or int(row['device_id'])!=source['training']['device_id']:
            raise ValueError('Metadata label/split/hand mismatch')
        session=(row['person'],row['session'])
        if session in owners and owners[session]!=row['split']:raise ValueError('Session crosses dataset splits')
        owners[session]=row['split']
        identity=(*session,row['gesture'],row['trial'])
        if identity in identities:raise ValueError('Duplicate recording identity')
        identities.add(identity)
    train=np.array([i for i,r in enumerate(rows) if r['split']=='train'])
    selected=np.array([i for i,r in enumerate(rows) if r['split']==split])
    if not len(train) or not len(selected):raise ValueError('Calibration/evaluation split is empty')
    mean,std=source['normalizer']
    normalized=((x-mean)/std).astype(np.float32)
    if not np.isfinite(normalized).all():raise ValueError('Nonfinite normalized input')
    return normalized[selected],y[selected],normalized[train],dict(
        evaluation_scope='recording-level '+split,calibration_scope='training split only',split=split,
        untouched_test_consumed=split=='test',data_sha256={str((path/n).resolve()):v for n,v in hashes.items()})

def pool(x):
    x=np.maximum(x,0)
    return x.reshape(x.shape[0],x.shape[1]//2,2).max(axis=2)

def float_layers(x,p):
    def conv(v,w,b):
        win=np.lib.stride_tricks.sliding_window_view(v,3,axis=1)
        return np.einsum('ctk,ock->ot',win,w,dtype=np.float64,optimize=False)+b[:,None]
    def linear(v,w,b):return np.einsum('od,d->o',w,v,dtype=np.float64,optimize=False)+b
    layers={'input':np.asarray(x,dtype=np.float64)}
    layers['conv1']=conv(layers['input'].T,p['conv1_w'],p['conv1_b']);layers['pool1']=pool(layers['conv1'])
    layers['conv2']=conv(layers['pool1'],p['conv2_w'],p['conv2_b']);layers['pool2']=pool(layers['conv2'])
    layers['fc1']=linear(layers['pool2'].ravel(),p['fc1_w'],p['fc1_b']);layers['hidden']=np.maximum(layers['fc1'],0)
    layers['logits']=linear(layers['hidden'],p['fc2_w'],p['fc2_b'])
    if any(not np.isfinite(v).all() for v in layers.values()):raise ValueError('Nonfinite float reference')
    return layers

def probabilities(logits):
    e=np.exp(logits-logits.max(axis=1,keepdims=True))
    return e/e.sum(axis=1,keepdims=True)

def profile(x,p):
    result={}
    for sample in x:
        for name,v in float_layers(sample,p).items():
            r=result.setdefault(name,dict(minimum=float('inf'),maximum=float('-inf'),elements=0))
            r['minimum']=min(r['minimum'],float(v.min()));r['maximum']=max(r['maximum'],float(v.max()));r['elements']+=v.size
    return result

def integer_bits(max_abs):
    if not math.isfinite(max_abs) or max_abs<0:raise ValueError('Invalid range')
    return 1 if max_abs==0 else math.floor(math.log2(max_abs))+2

def signed_width(lo,hi):
    bits=1
    while lo<-(1<<(bits-1)) or hi>(1<<(bits-1))-1:bits+=1
    return bits

def quantize(v,width,fraction):
    if not 2<=width<=32 or not 0<=fraction<=52:raise ValueError('Unsupported width/fraction')
    scaled=np.floor(np.asarray(v,dtype=np.float64)*(2.**fraction)+.5)
    if not np.isfinite(scaled).all():raise ValueError('Nonfinite quantization')
    lo,hi=-(1<<(width-1)),(1<<(width-1))-1
    clips=int(np.count_nonzero((scaled<lo)|(scaled>hi)))
    return np.clip(scaled,lo,hi).astype(np.int64),clips

def requantize(acc,weight_fraction,width):
    rounded=(acc+(1<<(weight_fraction-1)))>>weight_fraction if weight_fraction else acc
    lo,hi=-(1<<(width-1)),(1<<(width-1))-1
    clips=int(np.count_nonzero((rounded<lo)|(rounded>hi)))
    return np.clip(rounded,lo,hi).astype(np.int64),clips

def run_quantized(x,p,aw,af,ww,wf):
    q={};clips={};partial={};bounds={}
    for name,value in p.items():
        q[name],clips[name]=quantize(value,ww if name.endswith('_w') else aw,wf if name.endswith('_w') else af)
    # Full representable operand ranges, not measured activations, bound each dot.
    # Biases use this model's actual quantized values. Include rounding addition.
    for layer in ('conv1','conv2','fc1','fc2'):
        n=int(np.prod(q[layer+'_w'].shape[1:]))
        bias=max(abs(int(v)) for v in q[layer+'_b'].ravel())<<wf
        bound=n*(1<<(aw-1))*(1<<(ww-1))+bias+((1<<(wf-1)) if wf else 0)
        bounds[layer]=signed_width(-bound,bound)
        if bounds[layer]>64:raise ValueError('Analytic sum bound exceeds int64; simulator refuses unsafe accumulation')
    def dot(values,weights,bias,layer):
        products=weights[:,None,:]*values[None,:,:]
        prefix=np.cumsum(products,axis=-1,dtype=np.int64)+(bias<<wf)[:,None,None]
        lo=min(int(prefix.min()),min(int(v)<<wf for v in bias));hi=max(int(prefix.max()),max(int(v)<<wf for v in bias))
        r=partial.setdefault(layer,dict(minimum=lo,maximum=hi))
        r['minimum']=min(r['minimum'],lo);r['maximum']=max(r['maximum'],hi)
        out,count=requantize(prefix[:,:,-1],wf,aw);clips[layer+'_output']=clips.get(layer+'_output',0)+count
        return out
    logits=[]
    for sample in x:
        v,count=quantize(sample,aw,af);clips['input']=clips.get('input',0)+count;v=v.T
        for layer in ('conv1','conv2'):
            windows=np.lib.stride_tricks.sliding_window_view(v,3,axis=1)
            values=windows.transpose(1,0,2).reshape(windows.shape[1],-1)
            v=pool(dot(values,q[layer+'_w'].reshape(q[layer+'_w'].shape[0],-1),q[layer+'_b'],layer))
        v=dot(v.ravel()[None,:],q['fc1_w'],q['fc1_b'],'fc1')[:,0];v=np.maximum(v,0)
        v=dot(v[None,:],q['fc2_w'],q['fc2_b'],'fc2')[:,0]
        logits.append(v/(2.**af))
    for layer,r in partial.items():r['measured_partial_sum_bits']=signed_width(r['minimum'],r['maximum'])
    return np.asarray(logits),dict(clipping_events=sum(clips.values()),clipping_by_stage=clips,
                                  partial_sum_ranges=partial,full_range_accumulator_bits=bounds)

def compare(reference,actual,labels,threshold=.70,rule='ge'):
    if actual.shape!=reference.shape or not np.isfinite(actual).all():raise ValueError('Invalid candidate outputs')
    fp=probabilities(reference);qp=probabilities(actual);f=fp.argmax(axis=1);q=qp.argmax(axis=1)
    emit=lambda v:v>=threshold if rule=='ge' else v>threshold
    fe,qe=emit(fp.max(axis=1)),emit(qp.max(axis=1))
    changed=(fe!=qe)|(fe&qe&(f!=q))
    return dict(samples=len(labels),class_agreement=int(np.sum(f==q)),class_changes=int(np.sum(f!=q)),
                float_correct=int(np.sum(f==labels)),quantized_correct=int(np.sum(q==labels)),
                threshold_decision_changes=int(np.sum(fe!=qe)),emitted_label_changes=int(changed.sum()),
                max_probability_error=float(np.max(np.abs(qp-fp))),max_logit_error=float(np.max(np.abs(actual-reference))),
                changed_class_indices=np.flatnonzero(f!=q).tolist(),changed_filtered_indices=np.flatnonzero(changed).tolist())

def torch_reference_check(x,p):
    import torch
    import torch.nn.functional as f
    torch.set_num_threads(1)
    tensor=lambda v:torch.from_numpy(np.ascontiguousarray(v,dtype=np.float32))
    with torch.no_grad():
        v=tensor(x.transpose(0,2,1))
        for layer in ('conv1','conv2'):v=f.max_pool1d(f.relu(f.conv1d(v,tensor(p[layer+'_w']),tensor(p[layer+'_b']))),2)
        v=f.relu(f.linear(v.flatten(1),tensor(p['fc1_w']),tensor(p['fc1_b'])))
        return f.linear(v,tensor(p['fc2_w']),tensor(p['fc2_b'])).numpy()

def parse_widths(text):
    result=sorted(set(int(s) for s in text.split(',')),reverse=True)
    if not result or any(v<2 or v>32 for v in result):raise argparse.ArgumentTypeError('Widths must be comma-separated integers from 2 to 32')
    return result

def run(args):
    out=Path(args.out)
    if out.exists():raise FileExistsError('Choose a NEW output folder: '+str(out))
    source=load_source(args.weights,args.run)
    x,y,calibration,data_info=load_data(args.data,source,args.split)
    p=source['parameters'];ranges=profile(calibration,p)
    peak=max(max(abs(v['minimum']),abs(v['maximum'])) for v in ranges.values())
    ai=args.activation_int_bits if args.activation_int_bits is not None else max(1,integer_bits(peak))+args.guard_bits
    weight_peak=max(float(np.max(np.abs(p[n]))) for n in NAMES if n.endswith('_w'))
    wi=args.weight_int_bits if args.weight_int_bits is not None else integer_bits(weight_peak)
    reference=np.array([float_layers(v,p)['logits'] for v in x])
    report=dict(version=1,status='running',scope=source['mode'],features=source['features'],classes=source['classes'],
                source_sha256=source['provenance'],data=data_info,normalization_applied=source['normalizer'] is not None,
                calibration_samples=len(calibration),evaluation_samples=len(x),
                calibration_float_ranges=ranges,weight_ranges={n:dict(minimum=float(p[n].min()),maximum=float(p[n].max())) for n in NAMES},
                activation_integer_bits=ai,weight_integer_bits=wi,
                activation_integer_source='explicit' if args.activation_int_bits is not None else 'training/calibration float range plus guard bits',
                guard_bits_applied=0 if args.activation_int_bits is not None else args.guard_bits,
                float_reference='NumPy float64 GestureCNN; no hardware timing prediction',
                emission_semantics='Confidence-filtered class labels only; no stable-confirmation or game logic simulated',
                numeric_contract='floor(x*2**F+0.5); same-F activations/bias; exact int64 partial sums; one round/saturate per layer',
                threshold=args.threshold,threshold_rule=args.threshold_rule,max_probability_error_limit=args.max_probability_error,
                limitations=['No HLS resource, clock, latency or power measurements','No rolling-window or end-to-end timing simulation','Software passing candidates require further data and hardware validation',
                             'Full-range accumulator bounds use representable operands plus this model quantized biases; measured widths are not safe hardware limits'],
                candidates=[])
    if source['mode']=='three_flex_run':
        torch_logits=torch_reference_check(x,p)
        if not np.allclose(reference,torch_logits,rtol=2e-4,atol=2e-4) or not np.array_equal(reference.argmax(1),torch_logits.argmax(1)):
            raise ValueError('NumPy reference disagrees with PyTorch; precision sweep aborted')
        report['pytorch_reference_check']=dict(class_agreement=len(x),max_logit_error=float(np.max(np.abs(reference-torch_logits))))
    out.mkdir(parents=True)
    write_json(out/'range_profile.json',{k:v for k,v in report.items() if k!='candidates'})
    specs=[('current_numeric_baseline',32,18,22,21)]
    specs += [('candidate',aw,aw-ai,ww,ww-wi) for aw in args.activation_widths for ww in args.weight_widths]
    try:
        for kind,aw,af,ww,wf in specs:
            row=dict(kind=kind,activation_bits=aw,activation_integer_bits=aw-af,activation_fraction_bits=af,
                     weight_bits=ww,weight_integer_bits=ww-wf,weight_fraction_bits=wf)
            try:
                if not 0<=af<=52 or not 0<=wf<=52:raise ValueError('Width cannot represent the requested integer/fraction allocation')
                logits,numeric=run_quantized(x,p,aw,af,ww,wf)
                row.update(numeric);row.update(compare(reference,logits,y,args.threshold,args.threshold_rule))
                row['passes_regression_screen']=(row['clipping_events']==0 and row['class_changes']==0 and
                    row['emitted_label_changes']==0 and row['max_probability_error']<=args.max_probability_error)
                print(f"A{aw}/F{af} W{ww}/F{wf}: agreement={row['class_agreement']}/{len(y)}, "
                      f"filtered_changes={row['emitted_label_changes']}, clipping={row['clipping_events']}, "
                      f"max_probability_error={row['max_probability_error']:.6f}, screen={row['passes_regression_screen']}",flush=True)
            except ValueError as error:
                row.update(error=str(error),passes_regression_screen=False)
                print(f'A{aw}/F{af} W{ww}/F{wf}: REJECTED: {error}',flush=True)
            report['candidates'].append(row);write_json(out/'sweep.json',report)
        report['status']='completed'
    except BaseException:
        report['status']='interrupted';raise
    finally:
        write_json(out/'sweep.json',report)
        write_json(out/'range_profile.json',{k:v for k,v in report.items() if k!='candidates'})
        fields=['kind','activation_bits','activation_integer_bits','activation_fraction_bits','weight_bits','weight_integer_bits',
                'weight_fraction_bits','class_agreement','class_changes','threshold_decision_changes','emitted_label_changes',
                'clipping_events','max_probability_error','max_logit_error','float_correct','quantized_correct','passes_regression_screen','error']
        with (out/'candidates.csv').open('w',newline='',encoding='utf-8') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(report['candidates'])
    print('Saved reports:',out,flush=True)
    return report

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--weights',type=Path,help='Existing paired eight-class exported NPZ')
    source.add_argument('--run',type=Path,help='Trained three-flex run with checkpoint/config/normalization')
    p.add_argument('--data',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--split',choices=['train','validation','test'],default='validation')
    p.add_argument('--activation-widths',type=parse_widths,default=[24,20,18,16])
    p.add_argument('--weight-widths',type=parse_widths,default=[16,12,10,8])
    p.add_argument('--activation-int-bits',type=int,default=None)
    p.add_argument('--weight-int-bits',type=int,default=None)
    p.add_argument('--guard-bits',type=int,default=1,help='Extra activation range bits for automatic range selection')
    p.add_argument('--threshold',type=float,default=.70)
    p.add_argument('--threshold-rule',choices=['ge','gt'],default='ge',help='ge matches deployed worker; gt matches strictly-above display')
    p.add_argument('--max-probability-error',type=float,default=.01,help='Proposed absolute probability error limit, not a course requirement')
    args=p.parse_args(argv)
    if not 0<=args.threshold<=1 or not 0<=args.max_probability_error<=1 or not 0<=args.guard_bits<=8:
        p.error('Invalid threshold/probability-error limit/guard bits')
    run(args)

if __name__=='__main__':main()
