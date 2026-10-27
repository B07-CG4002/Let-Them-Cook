"""Deterministic software checks, explicitly synthetic and not accuracy evidence."""
import json
from pathlib import Path
import numpy as np
from src.hand_ai_flex3.core import LABELS,CHANNELS
from src.hand_ai_lab.data import write,sha
from src.hand_ai_lab.replay import replay,save_replay,sweep

class ScriptedBackend:
    def probabilities(self,device,window):
        index=int(round(float(window[-1,0])))
        if not 0<=index<7:raise ValueError('Synthetic label marker outside range')
        p=np.full(7,.05/6);p[index]=.95;return p

def make_capture(folder,split='validation',seed=42):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=False)
    rng=np.random.default_rng(seed);phases=['IDLE','PICKUP','DROP','CHOP','WASH','THROW','POURING','IDLE']
    meta=dict(type='capture',format=1,channels=CHANNELS,person='SYNTHETIC',session='synthetic_'+split,split=split,synthetic=True)
    samples=[];segments=[]
    for d in (1,2):
        for block,label in enumerate(phases):
            actual=phases[(block+2)%len(phases)] if d==1 else label
            segments.append(dict(id=f'{d}-{block}',device_id=d,start_s=float(block*4),end_s=float((block+1)*4),gesture=actual))
    for seq in range(1601):
        t=seq/50.
        for d in (1,2):
            block=min(int(t/4),7);label=phases[(block+2)%len(phases)] if d==1 else phases[block]
            v=rng.normal(0,.02,12);v[0]=LABELS.index(label);v[2]=1.;v[9:]=rng.uniform(.2,.8,3)
            samples.append(dict(type='sample',elapsed_s=t,device_id=d,boot_id=100+d,seq=seq,uptime_ms=seq*20,values=v.tolist()))
    capture=folder/'capture.jsonl'
    with capture.open('x',encoding='utf-8',newline='\n') as f:
        for row in [meta]+samples:f.write(json.dumps(row)+'\n')
    ann=folder/'annotations.json';write(ann,dict(format=1,capture_sha256=sha(capture),person=meta['person'],session=meta['session'],split=split,
                                               reviewed=True,segments=segments,synthetic=True))
    return capture,ann

def run(out):
    out=Path(out)
    if out.exists():raise FileExistsError(out)
    out.mkdir(parents=True)
    capture,annotations=make_capture(out/'input')
    report,records=replay(capture,ScriptedBackend(),{1,2},annotations)
    report['synthetic']=True;save_replay(out/'replay',report,records)
    selected=sweep(out/'replay',out/'sweep',thresholds=(.6,.7,.8),confirmations=(1,2,3),timeouts=(.6,.9))
    if report['counts']['prediction']<100 or report['metrics']['action_events']['false_negative']:
        raise AssertionError('Synthetic replay did not detect the scripted actions')
    result=dict(status='passed',synthetic=True,learned_model_accuracy_tested=False,fpga_tested=False,
                independent_hands=True,counts=report['counts'],metrics=report['metrics'],selected_validation_settings=selected,
                warning='Scores encode synthetic label markers; they do not measure a trained classifier.',
                timeline=str((out/'replay/timeline.html').resolve()))
    write(out/'summary.json',result);return result
