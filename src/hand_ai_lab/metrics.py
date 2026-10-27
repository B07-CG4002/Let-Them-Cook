"""Explicit, hand-independent offline metrics; unknown truth is never IDLE."""
import numpy as np
from src.hand_ai_flex3.core import LABELS
from src.hand_runtime import PULSES,ONGOING

def classify(truth,predicted):
    matrix=np.zeros((7,7),dtype=int)
    for a,b in zip(truth,predicted):matrix[LABELS.index(a),LABELS.index(b)]+=1
    per={}
    for i,label in enumerate(LABELS):
        tp=int(matrix[i,i]);support=int(matrix[i].sum());n=int(matrix[:,i].sum())
        precision=tp/n if n else 0.;recall=tp/support if support else 0.
        per[label]=dict(precision=precision,recall=recall,f1=2*precision*recall/(precision+recall) if precision+recall else 0.,support=support)
    represented=[r['f1'] for r in per.values() if r['support']]
    return dict(samples=int(matrix.sum()),accuracy=float(np.trace(matrix)/matrix.sum()) if matrix.sum() else None,
                macro_f1_present_classes=float(np.mean(represented)) if represented else None,
                class_order=LABELS,confusion_matrix=matrix.tolist(),per_class=per)

def segment_at(segments,d,t):
    return next((s for s in segments if s['device_id']==d and s['start_s']<=t<s['end_s']),None)

def training_label(segments,d,start,end):
    """One-shot: complete instance plus reviewed non-action context. Ongoing: pure."""
    covered=sorted((s for s in segments if s['device_id']==d and s['end_s']>start+1e-8 and s['start_s']<end-1e-8),key=lambda s:s['start_s'])
    cursor=start
    for s in covered:
        if s['gesture']=='UNKNOWN' or s['start_s']>cursor+1e-7:return None
        cursor=max(cursor,s['end_s'])
    if cursor<end-1e-7:return None
    actions=[s for s in covered if s['gesture'] in PULSES|ONGOING]
    if len(actions)==1 and actions[0]['gesture'] in PULSES:
        s=actions[0]
        return s if start<=s['start_s']+1e-8 and end>=s['end_s']-1e-8 else None
    if len(covered)==1 and covered[0]['gesture'] in ONGOING|{'IDLE'}:
        s=covered[0]
        return s if start>=s['start_s']-1e-8 and end<=s['end_s']+1e-8 else None
    return None

def score(records,annotations,late_tolerance=.5):
    if annotations is None:return dict(status='unscored',reason='No reviewed ground truth supplied; cues are not truth')
    segs=annotations['segments'];predictions=[r for r in records if r['type']=='prediction']
    evaluated=[];endpoint=[];semantic=[]
    for p in predictions:
        s=segment_at(segs,p['device_id'],p['elapsed_s'])
        if s and s['gesture'] in LABELS:endpoint.append((s['gesture'],p['gesture']))
        start=p.get('window_start_s')
        if s and s['gesture'] in LABELS and start is not None and start>=s['start_s']-1e-8:
            evaluated.append((s['gesture'],p['gesture']))
        if start is not None:
            target=training_label(segs,p['device_id'],start,p['elapsed_s'])
            if target:semantic.append((target['gesture'],p['gesture']))
    events=[dict(r,record_index=i) for i,r in enumerate(records) if r['type'] in ('action','state_start')]
    targets=[s for s in segs if s['gesture'] in PULSES|ONGOING]
    matched=set();matches=[];false=[];unscored=[];duplicates=[]
    for e in sorted(events,key=lambda e:(e['elapsed_s'],e['record_index'])):
        d=e['device_id'];t=e['elapsed_s'];g=e['gesture']
        current=segment_at(segs,d,t)
        if current and current['gesture']=='UNKNOWN':unscored.append(e);continue
        candidates=[s for s in targets if s['device_id']==d and s['gesture']==g and
                    s['start_s']<=t<s['end_s']+late_tolerance]
        available=sorted((s for s in candidates if s['id'] not in matched),key=lambda s:s['end_s'])
        if available:
            s=available[0];matched.add(s['id']);matches.append(dict(annotation_id=s['id'],event=e,
                onset_to_accept_s=t-s['start_s'],accepted_after_segment_end=t>=s['end_s']))
        elif current or candidates:
            false.append(e)
            if candidates:duplicates.append(e)
        else:unscored.append(e)
    missing=[s for s in targets if s['id'] not in matched]
    # Exposure is the sum across hands of reviewed intervals. It is hand-minutes,
    # not wall-clock minutes; unknown and unannotated time is excluded.
    exposure=sum(s['end_s']-s['start_s'] for s in segs if s['gesture']!='UNKNOWN')
    tp,fp,fn=len(matches),len(false),len(missing)
    precision=tp/(tp+fp) if tp+fp else None;recall=tp/(tp+fn) if tp+fn else None
    delays=[m['onset_to_accept_s'] for m in matches]
    return dict(status='scored',pure_window_classification=classify(*zip(*evaluated)) if evaluated else classify([],[]),
        endpoint_classification_diagnostic=classify(*zip(*endpoint)) if endpoint else classify([],[]),
        training_semantics_window_classification=classify(*zip(*semantic)) if semantic else classify([],[]),
        action_events=dict(true_positive=tp,false_positive=fp,false_negative=fn,
            precision=precision,recall=recall,f1=2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None,
            duplicate_acceptances=len(duplicates),unscored_acceptances=len(unscored),
            reviewed_hand_seconds=exposure,false_acceptances_per_hand_minute=fp*60/exposure if exposure else None,
            median_onset_to_accept_s=float(np.median(delays)) if delays else None,
            p95_onset_to_accept_s=float(np.percentile(delays,95)) if delays else None,
            late_tolerance_s=late_tolerance,matches=matches,missed=missing,false_acceptances=false),
        notes=['One expected activation per annotated action instance. IDLE/BACKGROUND require no action.',
               'UNKNOWN and unannotated time are unscored. Endpoint labels can mix actions; pure-window metrics are stricter.',
               'Offline onset-to-accept uses the recorded callback timeline and virtual scheduling, not measured live end-to-end latency.'])
