"""Deterministic callback-time replay through the existing independent-hand engine."""
from collections import defaultdict,deque
import json
import math
from pathlib import Path
import numpy as np
from src.hand_runtime import HandEngine,HandWindow,Job,ActionFilter
from src.hand_ai_flex3.core import LABELS,CHANNELS,resample
from src.hand_ai_lab.data import load_capture,load_annotations,sha,write
from src.hand_ai_lab.metrics import score

class ResearchWindow(HandWindow):
    """Explicit lab-only duration experiment, retaining 50 positions and 12 channels."""
    def __init__(self,device,seconds=2.):
        if seconds not in (.5,1.,1.5,2.):raise ValueError('Research duration must be 0.5, 1, 1.5 or 2 seconds')
        self.seconds=seconds;super().__init__(device)
    def job(self,now):
        if not self.previous or now-self.previous[3]>.5:return None
        start=self.elapsed-self.seconds
        if not self.rows or self.rows[0][0]>start+1e-8:return None
        data=list(self.rows)
        if sum(t>=start-1e-8 for t,v in data)<math.ceil(42.5*self.seconds):return None
        w=resample([t for t,v in data],[v for t,v in data],np.linspace(start,self.elapsed,50))
        boot,seq,uptime,received=self.previous
        return Job(self.device,boot,seq,uptime,self.generation,received,w)

class RunBackend:
    def __init__(self,runs,legacy11=False):
        self.predictors={};self.provenance={};self.seconds=None
        for d,path in runs.items():
            path=Path(path)
            if (path/'lab.json').exists():
                from src.hand_ai_lab.training import LabPredictor
                predictor=LabPredictor(path);seconds=predictor.contract['window_seconds'];kind='lab'
            else:
                if legacy11:
                    from src.hand_ai.pipeline import Predictor
                    kind='legacy11_diagnostic_only'
                else:
                    from src.hand_ai_flex3.pipeline import Predictor
                    kind='native12'
                predictor=Predictor(path);seconds=2.
            if predictor.device_id!=d:raise ValueError(f'Model hand mismatch for device {d}')
            if self.seconds is not None and seconds!=self.seconds:raise ValueError('Replay one duration contract at a time')
            self.seconds=seconds;predictor.torch.set_num_threads(1);self.predictors[d]=(predictor,kind)
            self.provenance[str(d)]=dict(run=str(path.resolve()),kind=kind,sha256={p.name:sha(p) for p in path.iterdir() if p.is_file()})
        if not self.predictors:raise ValueError('Supply at least one real hand run')
    def probabilities(self,device,window):
        p,kind=self.predictors[device]
        x=window[:,:11].copy() if kind=='legacy11_diagnostic_only' else window
        return p.probabilities(x)[0]

def replay(capture,backend,devices,annotations=None,threshold=.7,confirmations=3,interval=.2,timeout=.6,tick=.01,late_tolerance=.5,seconds=2.):
    if (not all(math.isfinite(x) and x>0 for x in (interval,timeout,tick)) or tick>interval or
            type(confirmations) is not int or not 1<=confirmations<=10 or not math.isfinite(threshold) or not 0<=threshold<=1):raise ValueError('Invalid replay/filter settings')
    if not devices or any(type(d) is not int or d not in (1,2) for d in devices):raise ValueError('Invalid model hands')
    meta,samples=load_capture(capture);truth=load_annotations(annotations,capture,meta,samples)
    engine=HandEngine(backend,threshold,confirmations,interval)
    if seconds!=2.:engine.windows={d:ResearchWindow(d,seconds) for d in (1,2)}
    engine.filters={d:ActionFilter(threshold,confirmations,timeout) for d in (1,2)}
    histories={d:deque(maxlen=512) for d in (1,2)};spans={};records=[];health=defaultdict(int);prev={};seen={}
    tick_index=0;i=0;end=samples[-1]['elapsed_s'];final=end+max(timeout,.5)+2*tick
    while tick_index*tick<=final+1e-9:
        now=round(tick_index*tick,9)
        while i<len(samples) and samples[i]['elapsed_s']<=now+1e-9:
            row=samples[i];i+=1;d=row['device_id'];health[f'hand_{d}_packets']+=1
            key=(d,row['boot_id'],row['seq']);signature=(row['uptime_ms'],tuple(row['values']))
            if key in seen:
                if seen[key]!=signature:raise ValueError('Conflicting payload for one source identity')
                health['exact_duplicates']+=1
            seen[key]=signature
            # Bound audit fingerprint memory; the runtime separately rejects old packets.
            if len(seen)>8192:seen.pop(next(iter(seen)))
            if d in prev:
                p=prev[d]
                if row['boot_id']!=p['boot_id']:health['boot_changes']+=1
                elif (row['seq']-p['seq'])%2**32 not in (0,1):health['sequence_discontinuities']+=1
            prev[d]=row
            consume=row.get('consume_elapsed_s')
            if consume is not None:health['max_callback_to_consume_ms']=max(health['max_callback_to_consume_ms'],(consume-row['elapsed_s'])*1000)
            if d not in devices:continue
            before=engine.windows[d].generation;added=engine.add(row,row['elapsed_s'])
            w=engine.windows[d]
            if w.generation!=before:
                histories[d].clear();records.append(dict(type='source_reset',device_id=d,elapsed_s=row['elapsed_s']))
                health['runtime_source_resets']+=1
            if added:
                history=histories[d];history.append((w.elapsed,row['elapsed_s']))
                start=w.elapsed-seconds
                if history[0][0]<=start+1e-8:
                    spans[key]=float(np.interp(start,[a for a,b in history],[b for a,b in history]))
                if len(spans)>8192:spans.pop(next(iter(spans)))
        for record in engine.process_due(now):
            if record['type']=='prediction':record['window_start_s']=spans.get((record['device_id'],record['boot_id'],record['seq']))
            records.append(record)
        tick_index+=1
    scoped_truth=dict(truth,segments=[s for s in truth['segments'] if s['device_id'] in devices]) if truth else None
    metrics=score(records,scoped_truth,late_tolerance)
    report=dict(format=1,scope='offline callback-time replay; no network or hardware execution',capture_meta=meta,
        capture_sha256=sha(capture),annotation_sha256=sha(annotations) if annotations else None,
        settings=dict(threshold=threshold,confirmations=confirmations,interval_s=interval,timeout_s=timeout,tick_s=tick,
                      window_seconds=seconds,time_positions=50,strict_above_threshold=True),
        duration_s=end,logical_end_s=final,devices=sorted(devices),health=dict(health),metrics=metrics,
        hand_bounds={str(d):[min(s['elapsed_s'] for s in samples if s['device_id']==d),max(s['elapsed_s'] for s in samples if s['device_id']==d)] for d in set(s['device_id'] for s in samples)},
        scoring_hands=sorted(devices),
        actual_live_latency_measured=False,annotations=truth,
        counts={k:sum(r['type']==k for r in records) for k in ('prediction','action','state_start','state_end','idle','stale_suppressed','source_reset')})
    return report,records

def filter_predictions(records,settings,end,threshold,confirmations,timeout):
    """Recompute ONLY output confirmation; model/window schedule is fixed and recorded."""
    filters={d:ActionFilter(threshold,confirmations,timeout) for d in (1,2)}
    inputs=[r for r in records if r['type'] in ('prediction','source_reset')];inputs.sort(key=lambda r:r['elapsed_s'])
    out=[];i=0;tick=settings['tick_s'];limit=max(end,max((r['elapsed_s'] for r in inputs),default=0))+max(timeout,.5)+2*tick
    for n in range(math.ceil(limit/tick)+1):
        now=round(n*tick,9)
        # Replay source resets at their original callback time before that poll's predictions.
        while i<len(inputs) and inputs[i]['elapsed_s']<=now+1e-9:
            r=inputs[i];i+=1;d=r['device_id'];t=r['elapsed_s']
            events=filters[d].reset('source_reset') if r['type']=='source_reset' else filters[d].update(r['gesture'],r['confidence'],t)
            if r['type']=='prediction':out.append(r)
            out.extend(dict(e,device_id=d,elapsed_s=t) for e in events)
        for d in (1,2):out.extend(dict(e,device_id=d,elapsed_s=now) for e in filters[d].tick(now))
    return out

def save_replay(out,report,records):
    from src.hand_ai_lab.timeline import timeline
    out=Path(out)
    if out.exists():raise FileExistsError(out)
    out.mkdir(parents=True)
    write(out/'report.json',report)
    with (out/'records.jsonl').open('x',encoding='utf-8',newline='\n') as f:
        for r in records:f.write(json.dumps(r,allow_nan=False)+'\n')
    timeline(out/'timeline.html',report,records)

def sweep(folder,out,thresholds=(.6,.7,.8,.9),confirmations=(1,2,3),timeouts=(.6,.9,1.2)):
    folder=Path(folder);out=Path(out)
    if out.exists():raise FileExistsError(out)
    report=json.loads((folder/'report.json').read_text());truth=report.get('annotations')
    if report['capture_meta']['split']!='validation' or not truth:raise ValueError('Sweep requires reviewed VALIDATION truth; test/diagnostic is refused')
    truth=dict(truth,segments=[s for s in truth['segments'] if s['device_id'] in report['devices']])
    if not any(s['gesture'] in LABELS[:-1] for s in truth['segments']):raise ValueError('No action instances for parameter selection')
    from src.hand_ai_lab.data import rows
    records=list(rows(folder/'records.jsonl'));table=[]
    for threshold in thresholds:
        for count in confirmations:
            for timeout in timeouts:
                trace=filter_predictions(records,report['settings'],report['duration_s'],threshold,count,timeout)
                m=score(trace,truth)['action_events']
                table.append(dict(threshold=threshold,confirmations=count,timeout_s=timeout,**{k:m[k] for k in ('f1','false_acceptances_per_hand_minute','median_onset_to_accept_s','true_positive','false_positive','false_negative')}))
    table.sort(key=lambda r:(-(r['f1'] or 0),r['false_acceptances_per_hand_minute'] or 0,r['median_onset_to_accept_s'] if r['median_onset_to_accept_s'] is not None else float('inf')))
    out.mkdir(parents=True);write(out/'sweep.json',dict(scope='validation-only confirmation sweep, not window/model retraining',
        replay_report_sha256=sha(folder/'report.json'),records_sha256=sha(folder/'records.jsonl'),candidates=table,selected=table[0],
        deployment_changed=False,selection_warning='Freeze chosen settings before evaluating fresh test recordings.'))
    return table[0]
