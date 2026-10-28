"""Explicit v5 preparation, two-hand provisional training, evaluation and live CPU check."""
import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import json
from pathlib import Path
import time

from src.hand_ai_flex3 import wide_training as training


class LiveConsole:
    """Compact console output only; all inference records stay in JSONL."""
    HEADER = f'{"TIME(s)":>8}  {"HAND":<5}  {"TYPE":<8}  {"GESTURE":<8}  {"CONF":>7}  {"AI(ms)":>8}'

    def __init__(self,threshold=.7,emit=None):
        self.threshold=threshold
        self.emit=emit if emit is not None else lambda line:print(line,flush=True)
        self.last_prediction={}

    def show(self,row):
        kind=row['type'];device=row['device_id']
        confidence=row.get('confidence')
        if kind=='prediction':
            if confidence is None or not confidence>self.threshold:
                return
            now=row['elapsed_s']
            if now-self.last_prediction.get(device,float('-inf'))<1:
                return
            self.last_prediction[device]=now
        elif confidence is not None and not confidence>self.threshold:
            return
        names={'prediction':'PREDICT','action':'ACCEPT','state_start':'START',
               'state_end':'END','idle':'IDLE','source_reset':'RESET','stale_suppressed':'STALE'}
        label=names.get(kind,'STATUS')
        hand='LEFT' if device==1 else 'RIGHT'
        gesture=row.get('gesture') or '-'
        confidence_text=f'{confidence:.4f}' if confidence is not None else '-'
        inference=row.get('inference_ms')
        inference_text=f'{inference:.1f}' if inference is not None else '-'
        self.emit(f'{row["elapsed_s"]:8.1f}  {hand:<5}  {label:<8}  {gesture:<8}  {confidence_text:>7}  {inference_text:>8}')


async def live(a):
    from src.hand_runtime import HandEngine
    from src.hand_ai_flex3.wide_ble import connection
    from src.hand_ai_flex3.ble import ensure_healthy
    from src.hand_ai_lab.data import annotation_template
    devices={'BOTH':(1,2),'LEFT':(1,),'RIGHT':(2,)}[a.hands]
    models={d:training.Predictor(Path(a.runs)/('left' if d==1 else 'right')) for d in devices}
    if any(models[d].device_id!=d for d in devices):raise ValueError('Wrong-hand model')
    class Backend:
        def probabilities(self,device,window):return models[device].probabilities(window)[0]
    engine=HandEngine(Backend(),threshold=a.threshold,confirmations=3,interval=.2)
    out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    out.mkdir(parents=True);started=time.monotonic();counts={d:0 for d in devices};predictions={d:0 for d in devices}
    capture_meta=dict(type='capture',format=1,channels=training.CHANNELS,person=a.person,session=a.session,
        split='diagnostic',sensor_packet_version=5,scope='physical BLE callback-time capture with local CPU AI; no game commands')
    with (out/'capture_receive_order.jsonl').open('x') as capture,(out/'predictions.jsonl').open('x') as results:
        capture.write(json.dumps(capture_meta)+'\n');capture.flush()
        def clock():return time.monotonic()-started
        def record(row):results.write(json.dumps(row,allow_nan=False)+'\n');results.flush()
        console=LiveConsole(a.threshold)
        print(f'Predictions shown only when confidence > {a.threshold:.2f}. All predictions are saved to predictions.jsonl.',flush=True)
        print('PREDICT = model candidate; ACCEPT/START/IDLE = confirmed event; END/RESET/STALE = status.',flush=True)
        print(console.HEADER,flush=True)
        status='finished'
        async def receive(d,queue,health):
            while True:
                p,values,received=await asyncio.wait_for(queue.get(),3);ensure_healthy(health)
                row=dict(type='sample',elapsed_s=received-started,device_id=d,boot_id=p.boot_id,
                         seq=p.seq,uptime_ms=p.uptime_ms,values=list(values))
                capture.write(json.dumps(row,allow_nan=False)+'\n');capture.flush();counts[d]+=1
                engine.add(row,received-started)
        async def compute():
            while True:
                for row in await asyncio.to_thread(engine.process_due,clock(),clock):
                    record(row);d=row['device_id']
                    if row['type']=='prediction':
                        predictions[d]+=1
                    console.show(row)
                await asyncio.sleep(.02)
        try:
            async with AsyncExitStack() as stack:
                links={d:await stack.enter_async_context(connection(d)) for d in devices}
                tasks=[asyncio.create_task(receive(d,links[d][1],links[d][2])) for d in devices]
                tasks+=[asyncio.create_task(compute()),asyncio.create_task(asyncio.sleep(a.seconds))]
                try:
                    done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                    for task in done:await task
                finally:
                    for task in tasks:task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
        except BaseException:
            status='interrupted_or_failed';raise
        finally:
            engine.reset(clock())
            for row in engine.pending:record(row)
            training.write(out/'live_summary.json',dict(status=status,samples=counts,predictions=predictions,
                game_commands_sent=False,scope='local CPU recognition only; no FPGA/server/phone verification',
                model_sha256={str(d):training.sha(Path(a.runs)/('left' if d==1 else 'right')/'model.pth') for d in devices}))
    # Independent receive tasks can consume two hand queues in a different
    # cross-hand order. Preserve the original log and make an explicitly merged
    # callback-time capture for later annotations/replay; per-hand order is kept.
    rows=[json.loads(line) for line in (out/'capture_receive_order.jsonl').read_text().splitlines()][1:]
    rows.sort(key=lambda row:row['elapsed_s'])
    with (out/'capture.jsonl').open('x') as capture:
        for row in [capture_meta,*rows]:capture.write(json.dumps(row,allow_nan=False)+'\n')
    training.write(out/'annotations_template.json',annotation_template(out/'capture.jsonl',capture_meta))
    return dict(status=status,samples=counts,predictions=predictions,out=str(out.resolve()))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    pr=sub.add_parser('progress');pr.add_argument('--input',required=True)
    prep=sub.add_parser('prepare');prep.add_argument('--input',required=True);prep.add_argument('--out',required=True)
    tr=sub.add_parser('train');tr.add_argument('--data',required=True);tr.add_argument('--out',required=True)
    tr.add_argument('--epochs',type=int,default=30);tr.add_argument('--seeds',type=int,nargs='+',default=[42,73,101])
    ev=sub.add_parser('evaluate');ev.add_argument('--runs',required=True);ev.add_argument('--data',required=True)
    ev.add_argument('--split',choices=['validation','test'],required=True);ev.add_argument('--out',required=True);ev.add_argument('--allow-test-use',action='store_true')
    ex=sub.add_parser('export-fpga');ex.add_argument('--runs',required=True);ex.add_argument('--data',required=True);ex.add_argument('--out',required=True)
    li=sub.add_parser('live');li.add_argument('--runs',required=True);li.add_argument('--out',required=True)
    li.add_argument('--hands',choices=['BOTH','LEFT','RIGHT'],default='BOTH');li.add_argument('--seconds',type=float,default=120)
    li.add_argument('--threshold',type=float,default=.7);li.add_argument('--person',default='P01')
    li.add_argument('--session',default='P01_live_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    a=p.parse_args(argv)
    if a.command=='progress':result=training.progress(a.input)
    elif a.command=='prepare':result=training.prepare(a.input,a.out)
    elif a.command=='train':result=training.train(a.data,a.out,a.epochs,a.seeds)
    elif a.command=='evaluate':
        r=training.evaluate(a.runs,a.data,a.split,a.out,a.allow_test_use)
        result=dict(split=r['split'],test_use_recorded=r['test_use_recorded'],metrics={h:v['metrics'] for h,v in r['models'].items()})
    elif a.command=='export-fpga':
        from tools.hand_hls_prepare import prepare
        r=prepare(out=Path(a.out),left_run=Path(a.runs)/'left',right_run=Path(a.runs)/'right',
                  left_data=Path(a.data)/'left',right_data=Path(a.data)/'right')
        result=dict(status=r['status'],scope=r['scope'],board_validation_pending=True)
    else:
        if not 0<a.seconds<=3600 or not 0<=a.threshold<1:p.error('Invalid duration/threshold')
        try:result=asyncio.run(live(a))
        except KeyboardInterrupt:print('Live check stopped; partial logs retained.');return
    print(json.dumps(result,indent=2,allow_nan=False))


if __name__=='__main__':main()
