"""Read-only capture inventory and reversible paired-capture quarantine."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil

LABELS = ('PICKUP','DROP','CHOP','WASH','THROW','POURING','IDLE')
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()

def inventory(root):
    root = Path(root).resolve()
    if not root.is_dir(): raise ValueError(f'Dataset missing: {root}')
    rows=[]
    for p in sorted(root.rglob('*.csv')):
        side=p.with_suffix('.json')
        m=json.loads(side.read_text(encoding='utf-8'))
        if sha(p)!=m.get('csv_sha256'): raise ValueError(f'CSV hash mismatch: {p}')
        if m.get('gesture') not in LABELS or m.get('split') not in ('train','validation','test'):
            raise ValueError(f'Unexpected recording identity: {side}')
        if m.get('device_id') not in (1,2) or not m.get('capture_id'):
            raise ValueError(f'Missing hand/capture identity: {side}')
        marker=json.loads((p.parent/'session.json').read_text(encoding='utf-8'))
        if any(m.get(k)!=marker.get(k) for k in ('person','session','split','device_id','recording_contract')):
            raise ValueError(f'Session identity differs: {p}')
        rows.append(dict(meta=m,csv=p,sidecar=side))
    for p in root.rglob('*.json'):
        if p.name!='session.json' and not p.with_suffix('.csv').is_file():
            raise ValueError(f'Orphan/unknown JSON: {p}')
    groups=defaultdict(list)
    for r in rows: groups[r['meta']['capture_id']].append(r)
    for cap, group in groups.items():
        devices=[r['meta']['device_id'] for r in group]
        declared=group[0]['meta'].get('captured_devices',devices)
        if sorted(devices)!=sorted(declared) or len(set(devices))!=len(devices):
            raise ValueError(f'Incomplete or duplicate capture pair: {cap}')
        for r in group:
            m=r['meta']
            if m.get('captured_devices',declared)!=declared: raise ValueError(f'Pair differs: {cap}')
            for other in group:
                n=other['meta']
                if any(m.get(k)!=n.get(k) for k in ('person','session','split')):
                    raise ValueError(f'Pair session differs: {cap}')
                if n['device_id']!=m['device_id'] and m.get('other_gesture')!=n['gesture']:
                    raise ValueError(f'Other gesture differs: {cap}')
    return rows

def quarantine(root, capture, reason, backup_root, apply=False):
    if not reason.strip(): raise ValueError('A correction reason is required')
    root=Path(root).resolve(); backup_root=Path(backup_root).resolve()
    if backup_root.is_relative_to(root): raise ValueError('Backup must be outside the dataset')
    selected=[r for r in inventory(root) if r['meta']['capture_id']==capture]
    if not selected: raise ValueError('Capture ID not found; nothing changed')
    if not apply: return [str(r['csv']) for r in selected]
    dest=backup_root/('capture_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    dest.mkdir(parents=True,exist_ok=False); copied=[]
    for r in selected:
        for key in ('csv','sidecar'):
            p=r[key];q=dest/p.relative_to(root);q.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(p,q);h=sha(p)
            if sha(q)!=h: raise ValueError('Backup verification failed; originals retained')
            copied.append((p,q,h))
    (dest/'manifest.json').write_text(json.dumps(dict(capture_id=capture,reason=reason.strip(),original_root=str(root),
        files=[dict(original=str(p),backup=str(q.relative_to(dest)),sha256=h) for p,q,h in copied]),indent=2))
    try:
        for p,q,h in copied:
            if sha(p)!=h: raise ValueError('Source changed during backup')
        for p,q,h in copied: p.unlink()
    except BaseException:
        for p,q,h in copied:
            if not p.exists(): shutil.copy2(q,p)
        raise
    return str(dest)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True)
    p.add_argument('--capture');p.add_argument('--reason',default='');p.add_argument('--apply',action='store_true')
    p.add_argument('--train-target',type=int,default=30)
    p.add_argument('--backup-root',default='.comms-local/backups/capture_review')
    a=p.parse_args(argv)
    if a.capture:
        print(quarantine(a.root,a.capture,a.reason,a.backup_root,a.apply));return
    if a.apply: p.error('--apply requires --capture and --reason')
    rows=inventory(a.root)
    if not rows: raise ValueError('No recordings found')
    people=sorted({r['meta']['person'] for r in rows});bad=False
    print('PERSON HAND GESTURE TRAIN VALIDATION TEST')
    for person in people:
        for device in (1,2):
            for gesture in LABELS:
                c=Counter(r['meta']['split'] for r in rows if r['meta']['person']==person and r['meta']['device_id']==device and r['meta']['gesture']==gesture)
                actual=[c[s] for s in ('train','validation','test')]
                bad |= actual != ([20,3,4] if a.train_target==20 else [a.train_target,4,6])
                print(person,device,gesture,*actual)
    print('Hash/pair/session integrity passed. Counts:', 'REVIEW' if bad else 'COMPLETE')
    print('This does not establish movement quality or recognition accuracy.')
    if bad: raise SystemExit(2)
if __name__=='__main__': main()
