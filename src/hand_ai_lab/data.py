"""Strict capture/annotation contracts and read-only recording audit."""
import csv
import hashlib
import json
import math
from collections import Counter,defaultdict
from pathlib import Path
import numpy as np
from src.hand_ai_flex3.core import CHANNELS,LABELS,CONFIG,resample

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8',newline='\n') as f:
        json.dump(value,f,indent=2,allow_nan=False);f.write('\n')
def rows(path):
    with Path(path).open(encoding='utf-8-sig') as f:
        for n,line in enumerate(f,1):
            if line.strip():
                try:yield json.loads(line)
                except ValueError as e:raise ValueError(f'Bad JSON line {n}: {path}') from e
def number(x):return type(x) in (int,float) and math.isfinite(x)
def uint(x):return type(x) is int and 0<=x<2**32
def identity(meta):
    for k in ('person','session'):
        if not isinstance(meta.get(k),str) or not meta[k].strip():raise ValueError('Need nonempty '+k)
    if meta.get('split') not in ('train','validation','test','diagnostic'):raise ValueError('Invalid split')

def load_capture(path):
    iterator=iter(rows(path));meta=next(iterator,None)
    if not isinstance(meta,dict) or meta.get('type')!='capture' or type(meta.get('format')) is not int or meta.get('format')!=1 or meta.get('channels')!=CHANNELS:
        raise ValueError('Need AI capture format 1 with twelve ordered physical-unit channels')
    identity(meta);samples=[];previous=-1.
    for row in iterator:
        if not isinstance(row,dict) or row.get('type')!='sample':raise ValueError('Capture expects sample rows')
        t=row.get('elapsed_s');v=row.get('values')
        if not number(t) or t<previous or t<0:raise ValueError('Capture arrival times must be finite, nonnegative and ordered')
        if type(row.get('device_id')) is not int or row['device_id'] not in (1,2):raise ValueError('Invalid hand')
        if not all(uint(row.get(k)) for k in ('boot_id','seq','uptime_ms')):raise ValueError('Invalid uint32 source')
        if not isinstance(v,list) or len(v)!=12 or not all(number(x) for x in v):raise ValueError('Need twelve finite physical-unit values')
        if 'consume_elapsed_s' in row and (not number(row['consume_elapsed_s']) or row['consume_elapsed_s']+1e-8<t):raise ValueError('Invalid callback/consume timing')
        samples.append(row);previous=t
    if not samples:raise ValueError('Capture has no sensor samples')
    return meta,samples

def annotation_template(capture,meta):
    return dict(format=1,capture_sha256=sha(capture),person=meta['person'],session=meta['session'],
                split=meta['split'],reviewed=False,segments=[],
                instructions='Use video/observation, not cue blocks, as truth. Segments are [start_s,end_s). '
                'BACKGROUND means reviewed non-action; UNKNOWN/unannotated time is excluded. '
                'Each action segment denotes one expected instance. Set reviewed=true only after review.')

def load_annotations(path,capture,meta,samples):
    if path is None:return None
    a=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if type(a.get('format')) is not int or a.get('format')!=1 or a.get('capture_sha256')!=sha(capture):raise ValueError('Annotation capture hash differs')
    if any(a.get(k)!=meta[k] for k in ('person','session','split')):raise ValueError('Annotation provenance differs')
    if a.get('reviewed') is not True:raise ValueError('Annotations must be explicitly reviewed=true')
    bounds={d:(min(s['elapsed_s'] for s in samples if s['device_id']==d),max(s['elapsed_s'] for s in samples if s['device_id']==d)) for d in set(s['device_id'] for s in samples)}
    seen=set();previous={1:-1.,2:-1.}
    if not isinstance(a.get('segments'),list):raise ValueError('Annotation segments must be an array')
    for seg in sorted(a['segments'],key=lambda x:(x.get('device_id',0),x.get('start_s',-1))):
        d=seg.get('device_id');start=seg.get('start_s');end=seg.get('end_s');label=seg.get('gesture')
        if type(d) is not int or d not in bounds or not number(start) or not number(end) or not 0<=start<end:
            raise ValueError('Invalid annotation hand/interval')
        if start<bounds[d][0]-1e-6 or end>bounds[d][1]+1e-6:raise ValueError('Annotation outside observed hand timeline')
        if start<previous[d]-1e-9:raise ValueError('Overlapping truth intervals for one hand')
        if not isinstance(label,str) or label not in LABELS+['BACKGROUND','UNKNOWN']:raise ValueError('Invalid truth label')
        if not isinstance(seg.get('id'),str) or not seg['id'] or seg['id'] in seen:raise ValueError('Need unique annotation IDs')
        seen.add(seg['id']);previous[d]=end
    return a

def import_triage(folder,out,person,session,split):
    folder=Path(folder);out=Path(out)
    if out.exists():raise FileExistsError(out)
    meta=dict(type='capture',format=1,channels=CHANNELS,person=person,session=session,split=split,
              scope='physical-unit callback-time replay; original source uint32 identities retained')
    identity(meta);all_rows=[];sources={}
    for hand,d in (('left',1),('right',2)):
        path=folder/(hand+'_raw.csv')
        if not path.exists():continue
        sources[path.name]=sha(path)
        with path.open(encoding='utf-8-sig',newline='') as f:
            for r in csv.DictReader(f):
                ints={}
                for k in ('device_id','boot_id','seq','uptime_ms'):
                    v=float(r[k])
                    if not math.isfinite(v) or v!=int(v):raise ValueError('Noninteger source identity')
                    ints[k]=int(v)
                if ints['device_id']!=d:raise ValueError(f'Wrong hand identity in {path.name}')
                row=dict(type='sample',elapsed_s=float(r['callback_elapsed_s']),**ints,
                         values=[float(r[k]) for k in CHANNELS])
                row['consume_elapsed_s']=float(r['consume_elapsed_s'])
                if row['consume_elapsed_s']+1e-8<row['elapsed_s']:raise ValueError('Consume time precedes callback')
                all_rows.append(row)
    if not all_rows:raise ValueError('No left_raw.csv/right_raw.csv samples found')
    # Sorting merges separate per-hand files; never reorder packets within a hand.
    for d in (1,2):
        times=[r['elapsed_s'] for r in all_rows if r['device_id']==d]
        if any(b<a for a,b in zip(times,times[1:])):raise ValueError('One hand callback clock moved backwards')
    all_rows.sort(key=lambda r:r['elapsed_s']);meta['source_sha256']=sources
    out.mkdir(parents=True);capture=out/'capture.jsonl'
    with capture.open('x',encoding='utf-8',newline='\n') as f:
        for r in [meta]+all_rows:f.write(json.dumps(r,allow_nan=False)+'\n')
    load_capture(capture)
    write(out/'annotations_template.json',annotation_template(capture,meta))
    cues=folder/'cues.jsonl'
    if cues.exists():write(out/'cue_reference.json',dict(warning='Cue intentions are not frame ground truth',source_sha256=sha(cues),cues=list(rows(cues))))
    write(out/'import.json',dict(source_directory=str(folder.resolve()),samples=len(all_rows),source_sha256=sources,capture_sha256=sha(capture)))
    return capture

def audit_recordings(folder):
    result=dict(scope='read-only raw three-flex recording audit',files=[],errors=[],warnings=[],counts={},features={})
    identities={};raw_hashes={};inputs={};groups={};people=defaultdict(set);values=defaultdict(list)
    paths=sorted(Path(folder).rglob('*.csv'))
    if not paths:raise ValueError('No CSV recordings found')
    counts=Counter()
    for path in paths:
        item=dict(path=str(path.resolve()),csv_sha256=sha(path));result['files'].append(item)
        try:
            m=json.loads(path.with_suffix('.json').read_text(encoding='utf-8-sig'));identity(m)
            if m.get('recording_contract')!=CONFIG['version']:raise ValueError('Not the three-flex recording contract')
            if item['csv_sha256']!=m.get('csv_sha256'):raise ValueError('CSV hash changed since recording')
            d=m.get('device_id');g=m.get('gesture');trial=m.get('trial')
            if type(d) is not int or d not in (1,2) or g not in LABELS or type(trial) is not int or trial<1:raise ValueError('Invalid hand/gesture/trial')
            with path.open(encoding='utf-8-sig',newline='') as f:table=list(csv.DictReader(f))
            t=np.array([float(r['elapsed_s']) for r in table]);v=np.array([[float(r[c]) for c in CHANNELS] for r in table])
            ids=np.array([[float(r[c]) for c in ('device_id','boot_id','seq','uptime_ms')] for r in table])
            if len(t)<85 or not np.isfinite(ids).all() or np.any(ids!=np.floor(ids)) or np.any(ids<0) or np.any(ids>=2**32):raise ValueError('Invalid source identity or fewer than 85 samples')
            if not np.all(ids[:,0]==d) or len(set(ids[:,1]))!=1:raise ValueError('Hand/boot changes within trial')
            dt=np.diff(ids[:,3].astype(np.int64))%2**32
            if np.any(np.diff(ids[:,2].astype(np.int64))%2**32!=1):raise ValueError('Sequence gap')
            if not np.allclose(t,np.r_[0.,np.cumsum(dt)/1000.],atol=1e-6) or not 1.85<=t[-1]<=2.1:raise ValueError('Two-second source timing mismatch')
            window=resample(t,v);fingerprint=hashlib.sha256(window.tobytes()).hexdigest()
            key=(d,m['person'],m['session'],g,trial);group=(m['person'],m['session'])
            if key in identities:raise ValueError('Duplicate recording identity: '+identities[key])
            if item['csv_sha256'] in raw_hashes:raise ValueError('Duplicate CSV bytes: '+raw_hashes[item['csv_sha256']])
            if fingerprint in inputs:raise ValueError('Duplicate resampled model input: '+inputs[fingerprint])
            if group in groups and groups[group]!=m['split']:raise ValueError('Person/session crosses dataset splits')
            identities[key]=str(path);raw_hashes[item['csv_sha256']]=str(path);inputs[fingerprint]=str(path);groups[group]=m['split']
            counts[f'{d}/{g}/{m["split"]}']+=1;people[m['split']].add(m['person']);values[d].append(v)
            item.update(valid=True,metadata=m,input_sha256=fingerprint,samples=len(t))
        except (ValueError,KeyError,OSError,TypeError) as e:
            item['valid']=False;item['error']=str(e);result['errors'].append(dict(path=str(path),error=str(e)))
    result['counts']=dict(counts)
    for d,arrays in values.items():
        v=np.concatenate(arrays);result['features'][str(d)]={c:dict(min=float(v[:,i].min()),max=float(v[:,i].max()),std=float(v[:,i].std()),span=float(np.ptp(v[:,i]))) for i,c in enumerate(CHANNELS)}
        for i,c in enumerate(CHANNELS):
            if np.ptp(v[:,i])<1e-4:result['warnings'].append(f'Hand {d}: {c} is nearly constant; inspect mounting/wiring. Raw flex values are ADC fractions, not calibrated bend fractions.')
    overlap=people['train']&people['test']
    if overlap:result['warnings'].append('Same people in train and test: valid for same-person/session evaluation, not evidence of unseen-person generalization: '+', '.join(sorted(overlap)))
    result['status']='failed' if result['errors'] else 'passed'
    return result
