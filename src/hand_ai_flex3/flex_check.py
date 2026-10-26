"""Guided BLE flex-range measurements. Reports candidates; never changes scaling.

python -m src.hand_ai_flex3.flex_check --hand both
Close the recording GUI first. All outputs are hardware checks, not training data.
"""
import argparse
import asyncio
from contextlib import AsyncExitStack
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import numpy as np
from src.hand_ai_flex3.ble import connection, collect, ensure_healthy
from src.hand_ai_flex3.core import CHANNELS, CONFIG, digest
from src.hand_ai_flex3.source_stats import SOURCE_STATS_UUID, parse_source_stats

ADDRESSES = {1:'38:18:2B:17:E2:E6', 2:'38:18:2B:14:EA:FE'}
FINGERS = [('flex1','index',34), ('flex2','middle',35), ('flex3','thumb',39)]


def summarize(open_trials, bent_trials):
    """Describe endpoint separation, noise and rails without changing any samples."""
    groups = []
    for trials in (open_trials, bent_trials):
        arrays = [np.asarray(t, dtype=float) for t in trials]
        if not arrays or any(a.ndim != 2 or a.shape[1] != 12 or len(a)<2
                             or not np.isfinite(a).all() for a in arrays):
            raise ValueError('Expected nonempty trials of finite (N,12) samples')
        groups.append(arrays)
    answer = {}
    for j, (channel, finger, gpio) in enumerate(FINGERS, start=9):
        ends = []
        for arrays in groups:
            values = np.concatenate([a[:,j] for a in arrays])
            ends.append(dict(median=float(np.median(values)),
                             p10=float(np.percentile(values,10)),
                             p90=float(np.percentile(values,90)),
                             within_hold_std=float(np.sqrt(np.mean([np.var(a[:,j]) for a in arrays]))),
                             repeat_median_std=float(np.std([np.median(a[:,j]) for a in arrays])),
                             adc_rail_fraction=float(np.mean((values<=.0001)|(values>=.9999)))))
        opened, bent = ends
        span = bent['median'] - opened['median']
        noise = max(opened['within_hold_std'], bent['within_hold_std'])
        overlap = max(opened['p10'],bent['p10']) <= min(opened['p90'],bent['p90'])
        warnings = []
        if abs(span) <= max(3*noise, 3/4095):
            warnings.append('Small endpoint separation relative to measured noise/resolution')
        if overlap:
            warnings.append('Straight and bent 10th-90th percentile bands overlap')
        if opened['adc_rail_fraction']>.1 or bent['adc_rail_fraction']>.1:
            warnings.append('Many samples at an ADC boundary; inspect motion response and circuit before scaling')
        answer[channel] = dict(finger=finger, gpio=gpio, straight=opened, bent=bent,
                               signed_span=float(span), span_adc_counts=float(abs(span)*4095),
                               separation_to_noise=float(abs(span)/max(noise,1/10000)),
                               endpoint_bands_overlap=bool(overlap), warnings=warnings,
                               candidate_only=dict(open=opened['median'], bent=bent['median'],
                                   formula='(raw - open) / (bent - open)' if abs(span)>1e-8 else None,
                                   applied=False))
    return answer


def save_rows(path, rows):
    with path.open('x', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['elapsed_s','device_id','boot_id','seq','uptime_ms',*CHANNELS])
        for elapsed,p,values in rows:
            writer.writerow([elapsed,p.device_id,p.boot_id,p.seq,p.uptime_ms,*values])


async def run(a):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    root = Path(a.out)/('flex_range_'+stamp)
    root.mkdir(parents=True, exist_ok=False)
    devices = [1,2] if a.hand=='both' else [1 if a.hand=='left' else 2]
    addresses = {1:a.left_address, 2:a.right_address}
    if len({addresses[d].upper() for d in devices}) != len(devices):
        raise ValueError('Selected gloves must have different Bluetooth addresses')
    report = dict(purpose='hardware_quality_check_not_training', created_utc=stamp,
                  recording_contract=CONFIG['version'], status='incomplete',
                  scaling_changed=False, repetitions=a.repeats, hands={})
    endpoints = {d:{'straight':[], 'bent':[]} for d in devices}
    try:
        async with AsyncExitStack() as stack:
            links = {}
            for device in devices:
                hand = 'LEFT' if device==1 else 'RIGHT'
                print(f'Connecting {hand}: {addresses[device]}', flush=True)
                client,q,health = await stack.enter_async_context(
                    connection(device, address=addresses[device], recording=True))
                links[device]=(client,q,health)
                stats=parse_source_stats(await client.read_gatt_char(SOURCE_STATS_UUID,use_cached=False),expected_device_id=device)
                report['hands'][hand]=dict(address=addresses[device],device_id=device,
                                         initial_source_stats=asdict(stats),trials=[])
            print('Both selected hands are checked independently. Hold poses still during GO.',flush=True)
            for repeat in range(1,a.repeats+1):
                for pose,instruction in [('straight','Hold instrumented fingers comfortably STRAIGHT'),
                                         ('bent','Bend instrumented fingers comfortably; do not force the sensors')]:
                    await asyncio.to_thread(input,f'\nRepeat {repeat}/{a.repeats}: {instruction}. Press Enter when ready. ')
                    for second in (3,2,1):
                        print(second,flush=True);await asyncio.sleep(1)
                    print('GO: hold still for 2 seconds',flush=True)
                    # Trial queues ignore preparation traffic; only real trial overflows count as errors.
                    result=await asyncio.gather(*(collect(links[d][1],links[d][2]) for d in devices),return_exceptions=True)
                    for device,rows in zip(devices,result):
                        hand='LEFT' if device==1 else 'RIGHT'
                        if isinstance(rows,BaseException):
                            raise RuntimeError(f'{hand} {pose} failed: {rows}') from rows
                        values=np.asarray([r[2] for r in rows],dtype=float)
                        if values.shape[1:]!=(12,) or not np.isfinite(values).all():
                            raise ValueError(f'{hand}: invalid sensor feature values')
                        endpoints[device][pose].append(values)
                        path=root/f'{hand.lower()}_{pose}_{repeat:02d}.csv'
                        save_rows(path,rows)
                        duration=rows[-1][0]
                        # Source samples include both endpoints, so use N-1 for the rate.
                        rate=(len(rows)-1)/duration
                        item=dict(pose=pose,repeat=repeat,file=path.name,sha256=digest(path),
                                  samples=len(rows),source_duration_s=duration,source_rate_hz=rate,
                                  first_seq=rows[0][1].seq,last_seq=rows[-1][1].seq,
                                  source_sequence_and_time_checks_passed=True)
                        report['hands'][hand]['trials'].append(item)
                        print(f'{hand}: {len(rows)} samples, {duration:.2f}s, source rate {rate:.1f} Hz; '
                              + ', '.join(f'{c} median={np.median(values[:,j]):.4f}' for j,c in enumerate(CHANNELS) if j>=9),flush=True)
            for device,(client,q,health) in links.items():
                ensure_healthy(health)
                hand='LEFT' if device==1 else 'RIGHT'
                end=parse_source_stats(await client.read_gatt_char(SOURCE_STATS_UUID,use_cached=False),expected_device_id=device)
                item=report['hands'][hand]
                if end.boot_id!=item['initial_source_stats']['boot_id']:
                    raise ValueError(f'{hand}: board rebooted during the check')
                item['final_source_stats']=asdict(end)
                item['reported_submission_failures_delta']=(end.failures-item['initial_source_stats']['failures'])%2**32
                item['reception_health']=dict(health)
                item['flex']=summarize(endpoints[device]['straight'],endpoints[device]['bent'])
            report['status']='measured'
    except BaseException as error:
        report['status']='failed';report['error']=f'{type(error).__name__}: {error}'
        raise
    finally:
        (root/'range_report.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
        print(f'\nReport: {root / "range_report.json"}',flush=True)
    for hand,item in report['hands'].items():
        print(f'\n{hand} — measured endpoints; calibration was NOT applied:',flush=True)
        for name,entry in item['flex'].items():
            print(f"  {name} ({entry['finger']}, GPIO{entry['gpio']}): straight={entry['straight']['median']:.4f}, "
                  f"bent={entry['bent']['median']:.4f}, span≈{entry['span_adc_counts']:.0f} ADC counts",flush=True)
            for warning in entry['warnings']:print('    REVIEW: '+warning,flush=True)
        if item['reported_submission_failures_delta']:
            print('  REVIEW: glove reports submission failures during the connected period.',flush=True)
    print('\nSend range_report.json for review before changing scaling or recording training data.',flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--hand',choices=['left','right','both'],default='both')
    p.add_argument('--repeats',type=int,default=3)
    p.add_argument('--out',default='data/hand_flex3_checks')
    p.add_argument('--left-address',default=ADDRESSES[1]);p.add_argument('--right-address',default=ADDRESSES[2])
    a=p.parse_args()
    if a.repeats<2:p.error('Use at least two repetitions to assess repeatability')
    try:asyncio.run(run(a))
    except KeyboardInterrupt:print('Stopped; completed check files remain saved.')

if __name__=='__main__':main()
