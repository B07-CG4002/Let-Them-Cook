"""Capture timed raw BLE streams and optional CPU-model predictions before handover.

No game commands are sent. --legacy11 explicitly omits flex3 ONLY from the
old model input; every raw recording retains all twelve physical channels.
"""
import argparse
import asyncio
from contextlib import AsyncExitStack
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from time import perf_counter, get_clock_info
import numpy as np
from src.hand_ai_flex3.ble import connection
from src.hand_ai_flex3.core import CHANNELS, digest
from src.hand_ai_flex3.live import HandWindow
from src.hand_ai_flex3.source_stats import SOURCE_STATS_UUID, parse_source_stats

ADDRESSES={1:'38:18:2B:17:E2:E6',2:'38:18:2B:14:EA:FE'}
# Cue labels are intended tasks, NOT frame-by-frame ground truth.
PLAN=[('IDLE',6),('PICKUP',10),('DROP',10),('CHOP',10),('THROW',10),
      ('WASH',10),('POURING',10),('IDLE',6)]

def row_json(stream,obj):
    stream.write(json.dumps(obj,allow_nan=False)+'\n');stream.flush()

def model_window(window, legacy11):
    x=np.asarray(window,dtype=np.float32)
    if x.shape!=(50,12) or not np.isfinite(x).all():raise ValueError('Expected finite (50,12) window')
    return x[:,:11].copy() if legacy11 else x.copy()

async def run(a):
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    root=Path(a.out)/('triage_'+stamp);root.mkdir(parents=True,exist_ok=False)
    summary=dict(purpose='integration_triage_not_training',created_utc=stamp,
                 scope='BLE to laptop CPU only; no Ultra96 or phone timing',
                 window_seconds=2,inference_interval_s=.2,legacy11=a.legacy11,
                 cue_labels_are_frame_ground_truth=False,status='incomplete',hands={})
    summary['clock_resolution_s']={name:get_clock_info(name).resolution
                                  for name in ('monotonic','perf_counter')}
    predictor=None
    if a.run:
        try:
            if a.legacy11:
                from src.hand_ai.pipeline import Predictor
                from src.hand_ai.core import LABELS
            else:
                from src.hand_ai_flex3.pipeline import Predictor
                from src.hand_ai_flex3.core import LABELS
            predictor=Predictor(a.run)
            predictor.torch.set_num_threads(1)
            summary['model']=dict(path=str(Path(a.run).resolve()),device_id=predictor.device_id,
                                  class_names=LABELS,
                                  hashes={p.name:digest(p) for p in Path(a.run).iterdir() if p.is_file()})
            print(f'Model loaded for device {predictor.device_id}; '+
                  ('11-feature diagnostic baseline; flex3 retained in raw CSV.' if a.legacy11 else '12-feature model.'),flush=True)
        except Exception as error:
            summary['model_error']=f'{type(error).__name__}: {error}'
            predictor=None
            print('Model unavailable; continuing with raw capture: '+summary['model_error'],flush=True)
    devices=[1,2] if a.hand=='both' else [1 if a.hand=='left' else 2]
    if predictor is not None and predictor.device_id not in devices:
        raise ValueError('Selected hand does not include the model training hand')
    try:
        async with AsyncExitStack() as stack:
            links={};windows={};latest={};counts={};cues=stack.enter_context((root/'cues.jsonl').open('x'))
            stopping=asyncio.Event()
            predictions=stack.enter_context((root/'predictions.jsonl').open('x'))
            start=time.monotonic();utc0=time.time();summary.update(clock_origin_monotonic=start,clock_origin_unix=utc0)
            for device in devices:
                hand='LEFT' if device==1 else 'RIGHT'
                client,q,health=await stack.enter_async_context(connection(device,ADDRESSES[device]))
                stats=parse_source_stats(await client.read_gatt_char(SOURCE_STATS_UUID,use_cached=False),expected_device_id=device)
                stream=stack.enter_context((root/f'{hand.lower()}_raw.csv').open('x',newline=''))
                writer=csv.writer(stream)
                writer.writerow(['callback_elapsed_s','consume_elapsed_s','device_id','boot_id','seq','uptime_ms',*CHANNELS])
                counts[device]=dict(packets=0,sequence_discontinuities=0,source_gaps_over_100ms=0,
                                    boot_changes=0,peak_callback_to_consume_ms=0.)
                summary['hands'][hand]=dict(address=ADDRESSES[device],initial_source_stats=asdict(stats))
                links[device]=(client,q,health,writer,stream);windows[device]=HandWindow()
            async def receive(device):
                client,q,health,writer,stream=links[device];previous=None
                try:
                    while not stopping.is_set():
                        packet,values,received=await asyncio.wait_for(q.get(),3)
                        consumed=time.monotonic();c=counts[device]
                        c['packets']+=1
                        c['peak_callback_to_consume_ms']=max(c['peak_callback_to_consume_ms'],(consumed-received)*1000)
                        if previous is not None:
                            c['boot_changes']+=int(packet.boot_id!=previous.boot_id)
                            c['sequence_discontinuities']+=int(packet.seq!=(previous.seq+1)%2**32)
                            dt=(packet.uptime_ms-previous.uptime_ms)%2**32
                            c['source_gaps_over_100ms']+=int(not 0<dt<=100)
                        writer.writerow([received-start,consumed-start,packet.device_id,packet.boot_id,packet.seq,packet.uptime_ms,*values])
                        if c['packets']%50==0:stream.flush()
                        windows[device].add(packet,values,received);latest[device]=(packet,received)
                        previous=packet
                        if any(health.values()):raise ValueError(f'BLE reception errors: {health}')
                finally:stream.flush()
            async def infer():
                while not stopping.is_set():
                    await asyncio.sleep(.2)
                    device=predictor.device_id
                    window=windows[device].window()
                    if window is None or device not in latest:continue
                    packet,received=latest[device]
                    if time.monotonic()-received>.5:continue
                    before=time.monotonic()
                    measured_before=perf_counter()
                    probabilities=await asyncio.to_thread(predictor.probabilities,model_window(window,a.legacy11))
                    measured_ms=(perf_counter()-measured_before)*1000
                    after=time.monotonic();prob=probabilities[0];k=int(prob.argmax());confidence=float(prob[k])
                    item=dict(elapsed_s=after-start,device_id=device,boot_id=packet.boot_id,
                              window_end_seq=packet.seq,window_end_uptime_ms=packet.uptime_ms,
                              window_start_uptime_ms=(packet.uptime_ms-2000)%2**32,
                              gesture=LABELS[k],confidence=confidence,probabilities=list(map(float,prob)),
                              inference_start_s=before-start,inference_ms=measured_ms,
                              callback_to_result_ms=(after-received)*1000,
                              displayed=confidence>.7 and after-received<=.5)
                    row_json(predictions,item)
                    if item['displayed']:print(f"AI device={device}: {LABELS[k]} {confidence:.2f} | infer={item['inference_ms']:.1f}ms",flush=True)
            async def cue():
                print('\nSTART: use the model hand (normally RIGHT) for each cue. Other hand may stay IDLE; WASH uses both.',flush=True)
                print('Within each action block: complete the action, pause, reset naturally, repeat. Record video if possible.',flush=True)
                for label,seconds in PLAN:
                    row_json(cues,dict(elapsed_s=time.monotonic()-start,cue=label,duration_s=seconds,
                                       meaning='intended task; preparation/return may occur inside block'))
                    print(f'\nCUE {label}: {seconds} seconds',flush=True)
                    try:
                        import winsound
                        await asyncio.to_thread(winsound.Beep,880,120)
                    except (ImportError,RuntimeError):pass
                    await asyncio.sleep(seconds)
                print('\nCAPTURE FINISHED',flush=True)
            readers=[asyncio.create_task(receive(d)) for d in devices]
            tasks=readers+([asyncio.create_task(infer())] if predictor else [])
            cue_task=asyncio.create_task(cue())
            try:
                done,_=await asyncio.wait([cue_task,*tasks],return_when=asyncio.FIRST_COMPLETED)
                for task in done:task.result()
                if cue_task not in done:raise RuntimeError('Capture task stopped before the cues completed')
                summary['status']='captured'
            finally:
                # An explicit stop flag also handles wait_for cancellation races:
                # a receiver that consumes one last packet must not loop forever.
                stopping.set()
                cue_task.cancel()
                for task in tasks:task.cancel()
                try:
                    await asyncio.wait_for(asyncio.gather(cue_task,*tasks,return_exceptions=True),5)
                finally:
                    # Preserve counters even if shutdown itself is interrupted.
                    for device,(_,_,health,_,stream) in links.items():
                        stream.flush()
                        hand='LEFT' if device==1 else 'RIGHT'
                        summary['hands'][hand].update(counts=dict(counts[device]),health=dict(health))
                for device,(client,q,health,writer,stream) in links.items():
                    hand='LEFT' if device==1 else 'RIGHT'
                    summary['hands'][hand].update(counts=counts[device],health=dict(health))
                    try:
                        raw=await asyncio.wait_for(client.read_gatt_char(SOURCE_STATS_UUID,use_cached=False),3)
                        end=parse_source_stats(raw,expected_device_id=device)
                        summary['hands'][hand]['final_source_stats']=asdict(end)
                    except Exception as error:summary['hands'][hand]['final_stats_error']=str(error)
            summary['elapsed_s']=time.monotonic()-start
    except BaseException as error:
        summary['status']='failed';summary['error']=f'{type(error).__name__}: {error}'
        raise
    finally:
        summary['files']={p.name:digest(p) for p in root.iterdir() if p.is_file() and p.name!='summary.json'}
        (root/'summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False),encoding='utf-8')
        print(f'\nSAVED: {root.resolve()}',flush=True)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--hand',choices=['left','right','both'],default='both')
    p.add_argument('--run');p.add_argument('--legacy11',action='store_true')
    p.add_argument('--out',default='data/hand_flex3_checks')
    a=p.parse_args()
    try:asyncio.run(run(a))
    except KeyboardInterrupt:print('Stopped; partial capture saved.')

if __name__=='__main__':main()
