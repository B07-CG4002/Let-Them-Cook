"""Validation-only fixed-point screens for native or lab hand models."""
import json
from pathlib import Path
import numpy as np
from src.hand_ai_lab.data import sha,write
from src.hand_ai_lab.training import load_dataset

def screen(run,data,out,activation_widths=(24,20,16),weight_widths=(16,12,10)):
    from tools import precision_sweep as q
    run=Path(run);out=Path(out)
    if out.exists():raise FileExistsError(out)
    if (run/'lab.json').exists():
        from src.hand_ai_lab.training import LabPredictor
        predictor=LabPredictor(run);x,y,meta,source=load_dataset(data)
        if source['sha256']!=predictor.contract['dataset']['sha256']:raise ValueError('Dataset provenance differs')
        state=predictor.model.state_dict()
        parameters=q.validate_parameters({n:state[k].detach().cpu().numpy() for n,k in zip(q.NAMES,q.STATE_KEYS)},12,7)
        normalized=(((x-predictor.mean)/predictor.std)*predictor.mask).astype(np.float32)
        cal=normalized[[i for i,m in enumerate(meta) if m['split']=='train']]
        ids=[i for i,m in enumerate(meta) if m['split']=='validation'];inputs=normalized[ids];targets=y[ids]
        provenance=dict(lab_sha256=sha(run/'lab.json'),payload_sha256=predictor.contract['payload_sha256'],data=source)
    else:
        model=q.load_source(run=run);inputs,targets,cal,data_info=q.load_data(data,model,'validation')
        parameters=model['parameters'];provenance=dict(model=model['provenance'],data=data_info)
    if not activation_widths or not weight_widths or any(type(w) is not int or not 2<=w<=32 for w in (*activation_widths,*weight_widths)):
        raise ValueError('Widths must be integer values between 2 and 32')
    ranges=q.profile(cal,parameters)
    peak=max(max(abs(v['minimum']),abs(v['maximum'])) for v in ranges.values())
    ai=max(1,q.integer_bits(peak))+1
    wi=q.integer_bits(max(float(np.max(np.abs(parameters[n]))) for n in q.NAMES if n.endswith('_w')))
    reference=np.array([q.float_layers(v,parameters)['logits'] for v in inputs])
    torch_reference=q.torch_reference_check(inputs,parameters)
    if not np.allclose(reference,torch_reference,atol=2e-4,rtol=2e-4) or not np.array_equal(reference.argmax(1),torch_reference.argmax(1)):
        raise ValueError('NumPy/PyTorch reference disagreement')
    result=dict(status='running',scope='training-calibrated, validation-evaluated numeric screen',test_used=False,
        provenance=provenance,activation_integer_bits_with_guard=ai,weight_integer_bits=wi,
        ranges=ranges,threshold=.7,threshold_rule='gt',candidates=[],hardware_programmed=False,
        warning='Candidate formats require matching HLS arithmetic/driver and hardware validation; no speed/resource gain is inferred.')
    out.mkdir(parents=True)
    try:
        for aw,af,ww,wf in [(32,18,22,21)]+[(a,a-ai,w,w-wi) for a in activation_widths for w in weight_widths]:
            row=dict(activation_bits=aw,activation_fraction=af,weight_bits=ww,weight_fraction=wf)
            try:
                if not 0<=af<=52 or not 0<=wf<=52:raise ValueError('Integer range does not fit width')
                logits,numeric=q.run_quantized(inputs,parameters,aw,af,ww,wf)
                row.update(numeric);row.update(q.compare(reference,logits,targets,.7,'gt'))
                row['passes_screen']=row['clipping_events']==0 and row['class_changes']==0 and row['emitted_label_changes']==0 and row['max_probability_error']<=.01
            except ValueError as e:row.update(error=str(e),passes_screen=False)
            result['candidates'].append(row);print(row,flush=True)
        result['status']='completed'
    except BaseException:
        result['status']='interrupted';raise
    finally:write(out/'precision.json',result)
    return result
