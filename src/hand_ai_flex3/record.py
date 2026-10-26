"""Interactive one-hand BLE recorder. No serial sensor parsing."""
import argparse
import asyncio
import csv
from datetime import datetime, timezone
from pathlib import Path
import re
import numpy as np
from src.hand_ai_flex3.core import CONFIG, CHANNELS, LABELS, SPLITS, write_json, digest, resample
from src.hand_ai_flex3.ble import connection, collect

def token(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise argparse.ArgumentTypeError('Use letters, digits, underscore or hyphen')
    return value

async def run(a):
    root = Path(a.out)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / 'session.json'
    session = dict(person=a.person, session=a.session, split=a.split, device_id=a.device,
                   recording_contract=CONFIG['version'])
    if marker.exists():
        import json
        if json.loads(marker.read_text()) != session:
            raise ValueError('Output folder belongs to a different hand/person/session/split')
    else:
        write_json(marker, session)
    async with connection(a.device, a.address, recording=True) as (_, queue, health):
        for trial in range(a.start, a.start + a.trials):
            stem = f'{a.person}_{a.session}_{a.gesture}_{trial:03d}'
            path = root / (stem + '.csv')
            sidecar = path.with_suffix('.json')
            if path.exists() or sidecar.exists():
                raise FileExistsError(f'Already recorded: {path}; choose another --start')
            await asyncio.to_thread(input, f'\n{a.gesture} trial {trial}: prepare, then press Enter. ')
            for second in (3,2,1):
                print(second, flush=True)
                await asyncio.sleep(1)
            print('GO: perform the gesture over this 2-second window', flush=True)
            rows = await collect(queue, health)
            t = [row[0] for row in rows]
            values = np.array([row[2] for row in rows])
            resample(t, values)
            with path.open('x', encoding='utf-8', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['elapsed_s','device_id','boot_id','seq','uptime_ms',*CHANNELS])
                for elapsed, p, v in rows:
                    writer.writerow([elapsed,p.device_id,p.boot_id,p.seq,p.uptime_ms,*v])
            write_json(sidecar, dict(**session, gesture=a.gesture, trial=trial,
                       created_utc=datetime.now(timezone.utc).isoformat(), csv_sha256=digest(path)))
            print(f'SAVED {path}: samples={len(rows)}, duration={t[-1]:.3f}s', flush=True)
            print('Channel min:', dict(zip(CHANNELS, values.min(axis=0).round(4))), flush=True)
            print('Channel max:', dict(zip(CHANNELS, values.max(axis=0).round(4))), flush=True)
            saturated = sum(any(v in (-32768,32767) for v in p.values) for _,p,_ in rows)
            if saturated:
                print(f'WARNING: {saturated} samples reach an int16 boundary; inspect firmware clamping before bulk recording.', flush=True)
            print('STOP. Rest before the next trial.', flush=True)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--device', type=int, choices=(1,2), default=2)
    p.add_argument('--address')
    p.add_argument('--person', type=token, required=True)
    p.add_argument('--session', type=token, required=True)
    p.add_argument('--split', choices=SPLITS, required=True)
    p.add_argument('--gesture', choices=LABELS, required=True)
    p.add_argument('--trials', type=int, default=1)
    p.add_argument('--start', type=int, default=1)
    p.add_argument('--out', required=True)
    a = p.parse_args()
    if a.trials < 1 or a.start < 1:
        p.error('trials and start must be positive')
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        print('Stopped. Only completed recordings were saved.')

if __name__ == '__main__':
    main()
