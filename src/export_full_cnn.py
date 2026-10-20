"""Export runtime parameters and exact integer reference for the full-CNN IP.
Requires only NumPy. Golden float path matches the existing board backend.
"""
import argparse
import json
from pathlib import Path
import struct
import numpy as np
NAMES=('conv1_w','conv2_w','fc1_w','fc2_w','conv1_b','conv2_b','fc1_b','fc2_b')
SHAPES=((32,22,3),(64,32,3),(64,704),(8,64),(32,),(64,),(64,),(8,))
WEIGHTS=53824
PARAMS=53992
ACT_FRAC=18
WEIGHT_FRAC=21

def encode(values,frac,width):
    q=np.floor(np.asarray(values,dtype=np.float64)*2**frac+.5)
    if not np.isfinite(q).all() or np.any(q<-(2**(width-1))) or np.any(q>2**(width-1)-1):
        raise ValueError(f'Value outside signed W{width}/F{frac} format; no silent clipping')
    return q.astype(np.int32)

def cast_activation(acc):
    rounded=(acc+(1<<20))>>21
    if np.any(rounded<-(1<<31)) or np.any(rounded>(1<<31)-1):
        raise OverflowError('Activation saturation: choose wider format and revalidate')
    return rounded.astype(np.int64)

def conv_integer(x,w,b):
    windows=np.lib.stride_tricks.sliding_window_view(x,3,axis=1)
    total=np.einsum('ctk,ock->ot',windows,w,dtype=np.int64,optimize=False)
    return cast_activation(total+b[:,None]*(1<<21))

def relu_pool(x):
    v=np.maximum(x,0); return v.reshape(v.shape[0],v.shape[1]//2,2).max(axis=2)

def integer_forward(x,p):
    c1=conv_integer(x.T,p['conv1_w'],p['conv1_b']); p1=relu_pool(c1)
    c2=conv_integer(p1,p['conv2_w'],p['conv2_b']); p2=relu_pool(c2)
    fc1=cast_activation(p['fc1_w']@p2.ravel()+p['fc1_b']*(1<<21)); h=np.maximum(fc1,0)
    logits=cast_activation(p['fc2_w']@h+p['fc2_b']*(1<<21))
    return logits,dict(conv1=c1,pool1=p1,conv2=c2,pool2=p2,fc1=fc1,relu_fc1=h,logits=logits)

def float_forward(x,p):
    def conv(v,w,b):
        win=np.lib.stride_tricks.sliding_window_view(v,3,axis=1)
        return np.einsum('ctk,ock->ot',win,w)+b[:,None]
    c1=conv(x.T,p['conv1_w'],p['conv1_b']); p1=relu_pool(c1)
    c2=conv(p1,p['conv2_w'],p['conv2_b']); p2=relu_pool(c2)
    fc1=p['fc1_w']@p2.ravel()+p['fc1_b'];h=np.maximum(fc1,0)
    logits=p['fc2_w']@h+p['fc2_b']
    return logits,dict(conv1=c1,pool1=p1,conv2=c2,pool2=p2,fc1=fc1,relu_fc1=h,logits=logits)

def main():
    a=argparse.ArgumentParser(description=__doc__)
    a.add_argument('--weights',required=True);a.add_argument('--test-data',required=True);a.add_argument('--output-dir',required=True)
    args=a.parse_args();out=Path(args.output_dir)
    if out.exists(): raise FileExistsError(f'Choose a new output directory: {out}')
    with np.load(args.weights,allow_pickle=False) as z:
        p={name:np.array(z[name],dtype=np.float32) for name in NAMES}
        config=json.loads(str(z['config_json']))
    if config['class_names']!=['PICKUP','DROP','CHOP','WASH','THROW','FIREEXT','POURING','IDLE']:
        raise ValueError('Wrong class order')
    for name,shape in zip(NAMES,SHAPES):
        if p[name].shape!=shape: raise ValueError(f'Wrong shape: {name}')
    q={name:encode(p[name],21 if name.endswith('_w') else 18,22 if name.endswith('_w') else 32).astype(np.int64) for name in NAMES}
    params=np.concatenate([q[name].ravel() for name in NAMES]).astype('<i4')
    assert len(params)==PARAMS
    with np.load(args.test_data,allow_pickle=False) as z:
        xs=np.array(z['inputs'],dtype=np.float32);labels=np.array(z['labels'],dtype=np.int64)
    if xs.shape!=(len(labels),50,22) or len(labels)==0 or np.any((labels<0)|(labels>=8)):
        raise ValueError('Invalid inputs/labels')
    inputs=encode(xs,18,32)
    integer=[];floating=[];ranges={};worst=0
    for n,x in enumerate(xs):
        fi,flayers=float_forward(x,p); qi,qlayers=integer_forward(inputs[n].astype(np.int64),q)
        integer.append(qi);floating.append(fi)
        error=float(np.max(np.abs(qi/2**18-fi)));worst=max(worst,error)
        for name,v in qlayers.items():
            r=ranges.setdefault(name,dict(min=float('inf'),max=float('-inf'),max_abs_error=0.0))
            r['min']=min(r['min'],float(v.min()/2**18));r['max']=max(r['max'],float(v.max()/2**18))
            r['max_abs_error']=max(r['max_abs_error'],float(np.max(np.abs(v/2**18-flayers[name]))))
    integer=np.array(integer,dtype='<i4');floating=np.array(floating,dtype='<f4')
    ip=integer.argmax(1);fp=floating.argmax(1)
    if not np.array_equal(ip,fp):
        raise ValueError(f'Quantization changes predictions for indices {np.flatnonzero(ip!=fp).tolist()}')
    out.mkdir(parents=True)
    params.tofile(out/'full_cnn_parameters.bin')
    with (out/'full_cnn_test.bin').open('wb') as f:
        f.write(struct.pack('<III',0x434E4E38,len(xs),PARAMS));params.tofile(f)
        for n in range(len(xs)):
            inputs[n].astype('<i4').tofile(f);integer[n].tofile(f);floating[n].tofile(f)
            f.write(struct.pack('<ii',int(labels[n]),int(fp[n])))
    np.savez_compressed(out/'full_cnn_board_vectors.npz',input_words=inputs.astype('<i4'),
        expected_words=integer,float_logits=floating,labels=labels,class_names=np.array(config['class_names']))
    report=dict(samples=len(xs),float_correct=int((fp==labels).sum()),fixed_correct=int((ip==labels).sum()),
        prediction_agreement=int((ip==fp).sum()),max_logit_error=worst,layer_ranges=ranges,
        input_format='signed32/F18',weight_format='signed22/F21',bias_output_format='signed32/F18',
        accumulator_format='signed64/F39',parameter_words=PARAMS,class_names=config['class_names'])
    (out/'full_cnn_reference_report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2));print('Wrote',out)
if __name__=='__main__':main()
