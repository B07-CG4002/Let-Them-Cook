"""Convert new combined two-glove CSVs into one 50x22 input per recording."""
import argparse
import json
import re
from pathlib import Path
import numpy as np
import pandas as pd
from src.ai_common import CLASS_NAMES,COLUMNS,CONFIG,resample
PATTERN = re.compile(r'^(P\d+)_(PICKUP|DROP|CHOP|WASH|THROW|FIREEXT|POURING|IDLE)_(\d+)\.csv$',re.I)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input-dir',required=True)
    p.add_argument('--output-dir',required=True)
    p.add_argument('--holdout-only',action='store_true',help='Read ONLY a holdout_trials folder, separately')
    a=p.parse_args(); root=Path(a.input_dir); output=Path(a.output_dir)
    if not root.is_dir(): raise FileNotFoundError(root)
    if output.exists(): raise FileExistsError(f'Choose a NEW output folder: {output}')
    if a.holdout_only and root.name.lower()!='holdout_trials':
        raise ValueError('--holdout-only requires the holdout_trials folder itself')
    X=[]; y=[]; rows=[]; seen=set()
    for path in sorted(root.rglob('*.csv')):
        if not a.holdout_only and any('holdout' in part.lower() for part in path.relative_to(root).parts):
            continue
        m=PATTERN.fullmatch(path.name)
        if not m:
            raise ValueError(f'Unexpected CSV filename (do not silently skip): {path}')
        person,gesture,trial=m.groups(); person=person.upper(); gesture=gesture.upper(); trial=int(trial)
        identity=(person,gesture,trial)
        if identity in seen: raise ValueError(f'Duplicate recording: {identity}')
        seen.add(identity)
        frame=pd.read_csv(path)
        missing=[c for c in ['elapsed_s']+COLUMNS if c not in frame]
        if missing: raise ValueError(f'{path}: missing columns {missing}')
        t=frame.elapsed_s.to_numpy(dtype=float); values=frame[COLUMNS].to_numpy(dtype=float)
        duration=t[-1]-t[0] if len(t)>1 else 0
        # Recorders usually cover ~1.97s of a 2s capture (last sample before stop).
        if not 1.85<=duration<=2.10: raise ValueError(f'{path}: expected ~2s; got {duration:.3f}s')
        X.append(resample(t,values)); y.append(CLASS_NAMES.index(gesture))
        rows.append(dict(person=person,gesture=gesture,trial=trial,source=str(path.resolve()),duration_s=duration))
    if not X: raise ValueError('No recordings found')
    if set(y)!=set(range(8)): raise ValueError('Dataset must contain all eight classes')
    output.mkdir(parents=True)
    np.save(output/'X.npy',np.asarray(X,dtype=np.float32)); np.save(output/'y.npy',np.asarray(y,dtype=np.int64))
    pd.DataFrame(rows).to_csv(output/'metadata.csv',index=False)
    (output/'config.json').write_text(json.dumps(CONFIG,indent=2),encoding='utf-8')
    print('Saved',output,'X:',np.asarray(X).shape)
    print(pd.DataFrame(rows).groupby('gesture').size().to_string())
    print('Holdout-only:',a.holdout_only)
if __name__=='__main__': main()
