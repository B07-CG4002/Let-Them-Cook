"""Audit native BLE trials for source integrity and gyro transport headroom.

This never changes recordings or certifies gesture accuracy/complete collection.
The stated IMU range is operator declared, not inferred from BLE v4 packets.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

CHANNELS = ('ax','ay','az','gx','gy','gz','yaw','pitch','roll','flex1','flex2','flex3')
HEADER = ('elapsed_s','device_id','boot_id','seq','uptime_ms',*CHANNELS)
LABELS = ('PICKUP','DROP','CHOP','WASH','THROW','POURING','IDLE')
UINT32 = 2**32


def uint(value, name):
    n = int(value)
    if str(n) != str(value) or not 0 <= n < UINT32:
        raise ValueError('Invalid uint32 '+name)
    return n


def audit_trial(path, device, session, *, sensor_version=4):
    path = Path(path)
    metadata = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
    if not isinstance(metadata,dict):raise ValueError('Trial metadata must be an object')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if metadata.get('csv_sha256') != digest:
        raise ValueError('CSV hash mismatch')
    if metadata.get('device_id') != device or type(metadata.get('device_id')) is not int:
        raise ValueError('Wrong hand identity')
    if metadata.get('session') != session or metadata.get('gesture') not in LABELS:
        raise ValueError('Wrong session/gesture')
    contract='independent_hand_flex3_v1' if sensor_version==4 else 'independent_hand_flex3_gyro_v5_v1'
    gyro_scale=100 if sensor_version==4 else 10
    if metadata.get('recording_contract') != contract:
        raise ValueError('Unreviewed recording contract')
    if sensor_version==5:
        source=metadata.get('acquisition_transport',{})
        if not isinstance(source,dict):raise ValueError('Acquisition metadata must be an object')
        if (source.get('sensor_packet_version')!=5 or source.get('gyro_scale')!=10 or
                source.get('imu_range_dps')!=1000 or source.get('source_stats_magic')!='LCS5'):
            raise ValueError('Missing or incompatible v5 acquisition provenance')
    if metadata.get('split') not in ('train','validation','test'):
        raise ValueError('Invalid split')
    with path.open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != HEADER:
            raise ValueError('Expected native twelve-channel CSV')
        rows = list(reader)
    if len(rows) < 85:
        raise ValueError('Too few samples')
    boot = uint(rows[0]['boot_id'], 'boot_id')
    previous = None
    elapsed = 0.
    channels = {c: [] for c in CHANNELS}
    for row in rows:
        if None in row or any(v is None for v in row.values()):
            raise ValueError('Malformed CSV row')
        ids = {c:uint(row[c],c) for c in ('device_id','boot_id','seq','uptime_ms')}
        if ids['device_id'] != device or ids['boot_id'] != boot:
            raise ValueError('Source identity changed')
        if previous is not None:
            dt = (ids['uptime_ms']-previous['uptime_ms']) % UINT32
            if (ids['seq']-previous['seq']) % UINT32 != 1 or not 0 < dt <= 100:
                raise ValueError('Sequence/timestamp discontinuity')
            elapsed += dt/1000.
        source_elapsed = float(row['elapsed_s'])
        if not math.isfinite(source_elapsed) or abs(source_elapsed-elapsed) > 1e-6:
            raise ValueError('Elapsed time differs from source time')
        for c in CHANNELS:
            value = float(row[c])
            if not math.isfinite(value):
                raise ValueError('Nonfinite '+c)
            scale = gyro_scale if c in ('gx','gy','gz') else (100 if c in ('yaw','pitch','roll') else 10000)
            raw = value*scale
            if raw < -32768-1e-6 or raw > 32767+1e-6 or abs(raw-round(raw)) > 1e-5:
                raise ValueError(f'Not a v{sensor_version} physical value: '+c)
            channels[c].append(value)
        previous = ids
    if not 1.85 <= elapsed <= 2.1:
        raise ValueError('Unexpected trial duration')
    axes = {}
    for c in ('gx','gy','gz'):
        values = channels[c]
        # Exact int16 rails identify clipping introduced by the unchanged wire.
        rails = sum(round(v*gyro_scale) in (-32768,32767) for v in values)
        axes[c] = dict(min=min(values), max=max(values),
                       peak_abs=max(map(abs,values)), wire_rail_samples=rails,
                       near_wire_limit_samples=sum(abs(v)>=(300 if sensor_version==4 else 900) for v in values))
    return dict(file=str(path),sha256=digest,gesture=metadata['gesture'],
                device_id=device,boot_id=boot,samples=len(rows),duration_s=elapsed,
                gyro=axes,
                other_channel_wire_rail_samples={c:sum(round(v*(100 if c in ('yaw','pitch','roll') else 10000))
                    in (-32768,32767) for v in channels[c]) for c in CHANNELS if c not in axes})


def audit(root, session, hands=('LEFT','RIGHT'), gestures=('WASH',), minimum=3, gyro_dps=500,sensor_version=4):
    if minimum < 1 or (sensor_version==4 and gyro_dps not in (250,500)) or (sensor_version==5 and gyro_dps!=1000) or sensor_version not in (4,5):
        raise ValueError('Invalid minimum/range')
    contract='independent_hand_flex3_v1' if sensor_version==4 else 'independent_hand_flex3_gyro_v5_v1'
    gyro_scale=100 if sensor_version==4 else 10
    if not session or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in session):
        raise ValueError('Use a session name, not a path')
    if not hands or len(set(hands)) != len(hands) or any(h not in ('LEFT','RIGHT') for h in hands):
        raise ValueError('Invalid selected hands')
    if not gestures or len(set(gestures)) != len(gestures) or any(g not in LABELS for g in gestures):
        raise ValueError('Invalid requested gestures')
    trials, errors, seen = [], [], set()
    counts = {h:{g:0 for g in gestures} for h in hands}
    for hand in hands:
        folder = Path(root)/hand.lower()/session
        if not folder.is_dir():
            errors.append(f'{hand}: session folder missing');continue
        try:
            marker = json.loads((folder/'session.json').read_text(encoding='utf-8'))
            if not isinstance(marker,dict):raise ValueError('Session marker must be an object')
            expected_device = 1 if hand == 'LEFT' else 2
            if marker.get('session') != session or marker.get('device_id') != expected_device or marker.get('recording_contract') != contract:
                raise ValueError('Session marker identity differs')
        except (OSError, ValueError) as error:
            errors.append(f'{hand}: {error}');continue
        for sidecar in folder.glob('*.json'):
            if sidecar.name != 'session.json' and not sidecar.with_suffix('.csv').is_file():
                errors.append(f'Orphan sidecar: {sidecar}')
        for path in sorted(folder.glob('*.csv')):
            try:
                trial = audit_trial(path,expected_device,session,sensor_version=sensor_version)
                meta = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
                if any(meta.get(k) != marker.get(k) for k in ('person','session','split','device_id','recording_contract')):
                    raise ValueError('Trial identity differs from session marker')
                if trial['sha256'] in seen:raise ValueError('Duplicate CSV content')
                seen.add(trial['sha256']);trials.append(trial)
                if trial['gesture'] in gestures:counts[hand][trial['gesture']] += 1
            except (OSError,ValueError,KeyError,TypeError) as error:
                errors.append(f'{path.name}: {error}')
    for hand, labels in counts.items():
        for gesture, count in labels.items():
            if count < minimum:errors.append(f'{hand} {gesture}: {count}/{minimum} required valid trials')
    rails = sum(a['wire_rail_samples'] for t in trials for a in t['gyro'].values())
    near = sum(a['near_wire_limit_samples'] for t in trials for a in t['gyro'].values())
    status = 'blocked' if errors or rails else ('review_headroom' if near else 'headroom_observed')
    return dict(status=status, scope='Diagnostic sample integrity and gyro headroom only; not model accuracy or final collection readiness',
                declared_sensor_range_dps=gyro_dps,firmware_range_verification='operator declared; v4 does not report IMU configuration',
                sensor_packet_version=sensor_version,
                wire_gyro_range_dps=[-32768/gyro_scale,32767/gyro_scale],
                near_limit_threshold_dps=300 if sensor_version==4 else 900,
                near_limit_scope='v4 wire headroom' if sensor_version==4 else 'v5 sensor headroom (wire is wider)',
                required_gestures=list(gestures),counts=counts,errors=errors,
                gyro_wire_rail_samples=rails,gyro_near_limit_samples=near,trials=trials,
                next_step=('Resolve invalid captures or transport clipping before full collection' if status=='blocked' else
                           'Review limited sensor/transport headroom before full collection' if status=='review_headroom' else
                           'Observed trials have headroom. Check the other intended gestures before full collection.'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',default='data/hand_flex3_checks/dual_pilot')
    p.add_argument('--session',required=True)
    p.add_argument('--hands',nargs='+',choices=['LEFT','RIGHT'],default=['LEFT','RIGHT'])
    p.add_argument('--gestures',nargs='+',choices=LABELS,default=['WASH'])
    p.add_argument('--minimum',type=int,default=3)
    p.add_argument('--gyro-dps',type=int,choices=[250,500,1000],default=500)
    p.add_argument('--sensor-version',type=int,choices=[4,5],default=4)
    p.add_argument('--out',required=True)
    a=p.parse_args()
    report=audit(a.root,a.session,a.hands,a.gestures,a.minimum,a.gyro_dps,a.sensor_version)
    # Never overwrite an earlier report or modify any recordings.
    out=Path(a.out)
    out.parent.mkdir(parents=True,exist_ok=True)
    with out.open('x',encoding='utf-8') as stream:json.dump(report,stream,indent=2,allow_nan=False)
    print(json.dumps({k:report[k] for k in ('status','counts','errors','gyro_wire_rail_samples','gyro_near_limit_samples','next_step')},indent=2))
    print('Report:',out.resolve())
    return 0 if report['status']=='headroom_observed' else 2


if __name__=='__main__':raise SystemExit(main())
