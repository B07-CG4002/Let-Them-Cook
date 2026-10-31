"""Resumable v5 Train -> Validation -> Test -> continuous edge recording GUI."""
from src.hand_ai_flex3.async_queue import queue_item
import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

TARGETS={'train':30,'validation':4,'test':6}
RECOVERY={'pickup_recovery':'PICKUP -> hold -> natural recovery',
          'drop_recovery':'DROP -> lift/retract empty hands',
          'throw_retract':'THROW -> return to rest'}
PAIRS={'lp_rd':('PICKUP','DROP'),'ld_rp':('DROP','PICKUP'),
       'lp_rc':('PICKUP','CHOP'),'lc_rp':('CHOP','PICKUP'),
       'lp_ri':('PICKUP','IDLE'),'li_rp':('IDLE','PICKUP'),
       'ld_rc':('DROP','CHOP'),'lc_rd':('CHOP','DROP'),
       'ld_ri':('DROP','IDLE'),'li_rd':('IDLE','DROP'),
       'lc_ri':('CHOP','IDLE'),'li_rc':('IDLE','CHOP')}

def token(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]+',value):raise ValueError('Use letters, digits, underscore or hyphen')
    return value

def session_name(person,day,campaign,split):
    return '_'.join(token(x) for x in (person,campaign,day,split))

def completed(root,person,session,gesture):
    """Count only verified CSV/sidecar pairs; never silently accept orphans/tampering."""
    root=Path(root);n=0
    for p in root.glob(f'{person}_{session}_{gesture}_*'):
        if p.suffix not in ('.csv','.json'):continue
        other=p.with_suffix('.json' if p.suffix=='.csv' else '.csv')
        if not other.is_file():raise ValueError(f'Orphan recording: {p}; review before resuming')
        if p.suffix=='.json':
            m=json.loads(p.read_text())
            if any(m.get(k)!=v for k,v in dict(person=person,session=session,gesture=gesture).items()):
                raise ValueError(f'Recording identity differs: {p}')
            if hashlib.sha256(other.read_bytes()).hexdigest()!=m['csv_sha256']:raise ValueError(f'Changed recording: {other}')
            n+=1
    return n

def remaining(target,done,batch):
    return min(batch,max(0,target-done))

def recording_gui(a):
    import tkinter as tk
    from tkinter import ttk,messagebox
    from src.hand_ai_flex3.wide_record_gui import WideApp,WideWorker
    from src.hand_ai_flex3.record_both_gui import pair_settings,validate_session_settings
    class App(WideApp):
        def __init__(self,root):
            self.auto_ready=False
            super().__init__(root,gyro_dps=a.gyro_dps)
            root.geometry('1080x950')
            self.vars['person'].set(a.person);self.vars['output'].set(a.output)
            self.vars['split'].set(a.stage);self.vars['left'].set('PICKUP');self.vars['right'].set('PICKUP')
            self.vars['count'].set(str(TARGETS[a.stage]))
            self.variant=tk.StringVar(value='')
            # Put required notes in the existing form, before capture buttons.
            # Packing after the expanding log can hide them below the window.
            form=self.field_widgets['output'].master
            note_row=form.grid_size()[1]
            ttk.Label(form,text='Variant / grip notes').grid(row=note_row,column=0,padx=8,pady=3)
            self.variant_entry=ttk.Entry(form,textvariable=self.variant,width=58)
            self.variant_entry.grid(row=note_row,column=1,pady=3,sticky='ew')
            self.inputs.append((self.variant_entry,None))
            self.progress=tk.StringVar()
            ttk.Label(form,textvariable=self.progress).grid(row=note_row+1,column=0,columnspan=2,pady=3)
            self.logbox.configure(height=4)
            for key in ('person','split'):
                self.vars[key].trace_add('write',self.update_session)
            for key in ('left','right','mode','output'):
                self.vars[key].trace_add('write',self.refresh_counts)
            self.auto_ready=True;self.update_session()
            self.phase.set('Counts are TARGETS; only remaining trials saved. Notes: record variant/grip per batch.')
        def update_session(self,*_):
            if not self.auto_ready:return
            try:
                split=self.vars['split'].get()
                self.vars['session'].set(session_name(self.vars['person'].get(),a.date,a.campaign,split))
                self.vars['count'].set(str(TARGETS[split]))
                self.refresh_counts()
            except (ValueError,KeyError):pass
        def refresh_counts(self,*_):
            if not self.auto_ready:return
            try:
                v={k:x.get() for k,x in self.vars.items()};parts=[]
                for hand,key in [('left','left'),('right','right')]:
                    n=completed(Path(v['output'])/hand/v['session'],v['person'],v['session'],v[key])
                    parts.append(f'{hand.upper()} {v[key]}: {n}/{TARGETS[v["split"]]}')
                self.progress.set(' | '.join(parts))
            except (ValueError,OSError,KeyError) as e:self.progress.set(str(e))
        def start(self):
            try:
                if not self.worker or not self.connected:return
                v={k:x.get() for k,x in self.vars.items()};split=v['split'];target=TARGETS[split]
                config=pair_settings(v['person'],v['session'],split,v['left'],v['right'],v['count'],v['output'])
                config=self.worker.prepare_config({d:config[d] for d in self.worker.devices})
                validate_session_settings(config)
                counts={d:remaining(target,completed(c['root'],c['person'],c['session'],c['gesture']),int(v['count'])) for d,c in config.items()}
                if len(set(counts.values()))!=1:
                    raise ValueError('Selected hands have different remaining counts. Resume the incomplete hand in LEFT/RIGHT only mode.')
                count=next(iter(counts.values()))
                if not count:
                    messagebox.showinfo('Target reached','Selected gesture(s) already meet the split target.');return
                if not self.variant.get().strip():raise ValueError('Enter variant/grip notes first (e.g. overhand; thumb straight).')
                for d in config:config[d].update(count=count,recording_variant=self.variant.get().strip())
                self.worker.cancel_recording.clear();self.busy=True
                self.worker.commands.put(dict(settings=config));self.controls()
            except (ValueError,OSError) as e:messagebox.showerror('Recording settings',str(e))
        def poll(self):
            if self.closing:return
            super().poll();self.refresh_counts()
    root=tk.Tk();App(root);root.mainloop()

async def capture_clip(folder,meta,links,seconds,emit=lambda s:None,*,gyro_dps=1000):
    """Keep partial evidence on any failure. Only complete validated clips get final names."""
    from src.hand_ai_flex3.ble import ensure_healthy
    from src.hand_ai_flex3.core import CHANNELS
    folder=Path(folder);partial=folder.with_name(folder.name+'.partial_'+str(time.time_ns()))
    partial.mkdir(parents=True,exist_ok=False)
    started=time.monotonic();counts={d:0 for d in links};previous={};rows=[]
    header=dict(type='capture',format=1,channels=CHANNELS,person=meta['person'],session=meta['session'],
                split='diagnostic',sensor_packet_version=5,case=meta['case'],repetition=meta['repetition'],
                intent=meta['intent'],cue_is_ground_truth=False,scope='raw continuous v5 capture; no inference or game commands')
    from src.hand_ai_flex3.wide_ble import transport_for
    header['acquisition_transport']=transport_for(gyro_dps)
    header['captured_devices']=list(links)
    status='partial';tasks=[]
    with (partial/'capture_receive_order.jsonl').open('x') as f:
        f.write(json.dumps(header)+'\n');f.flush()
        async def receive(d):
            q,health=links[d]
            while True:
                p,v,t=await queue_item(q,3)
                if t<started:continue
                if t-started>seconds:continue
                ensure_healthy(health)
                if p.device_id!=d:raise ValueError('Wrong hand in edge capture')
                old=previous.get(d)
                if old and (p.boot_id!=old.boot_id or (p.seq-old.seq)%2**32!=1 or not 0<(p.uptime_ms-old.uptime_ms)%2**32<=100):
                    raise ValueError(f'Hand {d}: source gap/reboot; partial clip retained')
                previous[d]=p
                row=dict(type='sample',elapsed_s=t-started,device_id=d,boot_id=p.boot_id,seq=p.seq,uptime_ms=p.uptime_ms,values=list(v))
                f.write(json.dumps(row,allow_nan=False)+'\n');f.flush();rows.append(row);counts[d]+=1
        tasks=[asyncio.create_task(receive(d)) for d in links]
        timer=asyncio.create_task(asyncio.sleep(seconds))
        try:
            done,_=await asyncio.wait([timer,*tasks],return_when=asyncio.FIRST_COMPLETED)
            for task in done:task.result()
            if timer not in done:raise ValueError('Receiver ended before capture duration')
            for d in links:
                ensure_healthy(links[d][1])
                ts=[r['elapsed_s'] for r in rows if r['device_id']==d]
                if len(ts)<seconds*40 or not ts or ts[-1]-ts[0]<seconds-.6:raise ValueError(f'Hand {d}: insufficient continuous coverage')
            status='complete'
        finally:
            for task in [timer,*tasks]:task.cancel()
            await asyncio.gather(timer,*tasks,return_exceptions=True)
            (partial/'status.json').write_text(json.dumps(dict(status=status,counts=counts,**meta),indent=2))
    with (partial/'capture.jsonl').open('x') as f:
        for row in [header,*sorted(rows,key=lambda x:x['elapsed_s'])]:f.write(json.dumps(row,allow_nan=False)+'\n')
    from src.hand_ai_lab.data import annotation_template
    (partial/'annotations_template.json').write_text(json.dumps(annotation_template(partial/'capture.jsonl',header),indent=2))
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in partial.iterdir() if p.is_file()}
    (partial/'complete.json').write_text(json.dumps(dict(files=hashes,**meta),indent=2))
    if folder.exists():raise FileExistsError(folder)
    partial.rename(folder);emit(f'SAVED {folder}')

def edge_done(folder):
    p=Path(folder)
    if not p.exists():return False
    m=json.loads((p/'complete.json').read_text())
    if any(not (p/n).is_file() or hashlib.sha256((p/n).read_bytes()).hexdigest()!=h for n,h in m['files'].items()):
        raise ValueError(f'Edge recording hash differs: {p}')
    return True

def edge_gui(a):
    import tkinter as tk
    from tkinter import ttk,messagebox
    import queue as thread_queue
    from src.hand_ai_flex3.record_gui import Worker
    from src.hand_ai_flex3.wide_ble import connection
    from src.hand_ai_flex3.core import CHANNELS
    from src.hand_ai_flex3.ble import ensure_healthy
    class EdgeWorker(Worker):
        def __init__(self,devices,case,count):
            super().__init__(0);self.devices=devices;self.case=case;self.requested=count;self.latest={}
        def snapshot(self):
            with self.lock:
                events=list(self.events);self.events.clear()
                return events,dict(self.latest),self.count
        async def session(self):
            async with AsyncExitStack() as stack:
                links={d:(await stack.enter_async_context(connection(d)))[1:] for d in self.devices}
                taps={d:(asyncio.Queue(maxsize=1024),links[d][1]) for d in self.devices};active=False
                async def receive(d):
                    while not self.stop.is_set():
                        p,v,t=await queue_item(links[d][0],3);ensure_healthy(links[d][1])
                        with self.lock:self.latest[d]=(p,v,t);self.count+=1
                        if active:taps[d][0].put_nowait((p,v,t))
                readers=[asyncio.create_task(receive(d)) for d in self.devices]
                async def record():
                    nonlocal active
                    case=self.case;saved=0
                    for rep in range(1,4):
                        mode='both' if self.devices==(1,2) else ('left' if self.devices==(1,) else 'right')
                        folder=Path(a.edges)/a.person/session_name(a.person,a.date,a.campaign,'diagnostic')/mode/case/f'{rep:03d}'
                        if edge_done(folder):self.event('saved',text=f'Already saved: {case} #{rep}');continue
                        if saved>=self.requested:break
                        for s in (3,2,1):
                            self.event('phase',text=f'PREP {s}: {case} #{rep}');await asyncio.sleep(1)
                        self.event('phase',text=f'GO {case} #{rep} — capture {a.edge_seconds:g}s, then STOP')
                        meta=dict(person=a.person,session=session_name(a.person,a.date,a.campaign,'diagnostic'),case=case,repetition=rep,
                                  intent=RECOVERY.get(case) or dict(zip(('LEFT','RIGHT'),PAIRS[case])))
                        for q,_ in taps.values():
                            while not q.empty():q.get_nowait()
                        active=True
                        try:await capture_clip(folder,meta,taps,a.edge_seconds,lambda s:self.event('saved',text=s),gyro_dps=a.gyro_dps)
                        finally:active=False
                        saved+=1
                    self.event('complete',text='Case finished. Select the next case; completed clips are retained.')
                job=asyncio.create_task(record())
                try:
                    done,_=await asyncio.wait([job,*readers],return_when=asyncio.FIRST_COMPLETED)
                    for task in done:task.result()
                    if job not in done:raise ValueError('BLE reader ended early')
                finally:
                    for task in [job,*readers]:task.cancel()
                    await asyncio.gather(job,*readers,return_exceptions=True)
    root=tk.Tk();root.title('V5 continuous edge recorder');root.geometry('1080x850')
    phase=tk.StringVar(value='READY — choose hands and edge case')
    ttk.Label(root,textvariable=phase,font=('Segoe UI',20)).pack(pady=12)
    fields={'mode':tk.StringVar(value='BOTH'),'person':tk.StringVar(value=a.person),
            'session':tk.StringVar(value=session_name(a.person,a.date,a.campaign,'diagnostic')),
            'case':tk.StringVar(value='pickup_recovery'),'count':tk.StringVar(value='3'),
            'output':tk.StringVar(value=a.edges)}
    form=ttk.Frame(root);form.pack();inputs=[]
    for row,(key,label,choices) in enumerate([
        ('mode','Capture hands',('LEFT only','RIGHT only','BOTH')),('person','Person ID',None),
        ('session','Session',None),('case','Edge case',tuple(RECOVERY)+tuple(PAIRS)),
        ('count','Additional clips (target 3 per case)',None),('output','Edge output root',None)]):
        ttk.Label(form,text=label).grid(row=row,column=0,padx=8,pady=3)
        w=ttk.Combobox(form,textvariable=fields[key],values=choices,state='readonly',width=60) if choices else ttk.Entry(form,textvariable=fields[key],width=63)
        w.grid(row=row,column=1,pady=3);inputs.append((w,choices,key))
    hint=tk.StringVar()
    def describe(*_):
        case=fields['case'].get()
        hint.set(RECOVERY.get(case) or 'LEFT '+PAIRS[case][0]+' / RIGHT '+PAIRS[case][1])
    fields['case'].trace_add('write',describe);describe()
    ttk.Label(root,textvariable=hint).pack(pady=5)
    ttk.Label(root,text=f'Preparation: 3 seconds | Capture: {a.edge_seconds:g} seconds | Video both hands and cues').pack()
    ttk.Label(root,text='Record 1 recovery case + 4 mixed cases, 3 clips each. Use BOTH for the full protocol.').pack()
    values={};statuses={}
    for d,hand in ((1,'LEFT'),(2,'RIGHT')):
        panel=ttk.LabelFrame(root,text=hand,padding=6);panel.pack(fill='x',padx=12,pady=5)
        statuses[d]=tk.StringVar(value='Disconnected');ttk.Label(panel,textvariable=statuses[d]).grid(row=0,column=0,columnspan=6)
        for i,c in enumerate(CHANNELS):
            row=1+2*(i//6);col=i%6;panel.columnconfigure(col,weight=1)
            ttk.Label(panel,text=c).grid(row=row,column=col)
            values[d,c]=tk.StringVar(value='—');ttk.Label(panel,textvariable=values[d,c],font=('Consolas',11)).grid(row=row+1,column=col,padx=8)
    logs=tk.Text(root,height=8);logs.pack(fill='both',expand=True,padx=12,pady=6)
    worker=None;closing=False
    def start():
        nonlocal worker
        if worker:return
        try:
            count=int(fields['count'].get())
            if not 1<=count<=3:raise ValueError('Choose 1..3 additional clips')
            if fields['person'].get()!=a.person or fields['session'].get()!=session_name(a.person,a.date,a.campaign,'diagnostic'):
                raise ValueError('Person/session are fixed by launch identity; relaunch with --person to change wearer')
            a.edges=fields['output'].get()
            if not a.edges.strip():raise ValueError('Enter an output root')
            devices={'LEFT only':(1,),'RIGHT only':(2,),'BOTH':(1,2)}[fields['mode'].get()]
            worker=EdgeWorker(devices,fields['case'].get(),count);worker.thread.start()
            for w,_,_ in inputs:w.config(state='disabled')
            start_button.config(state='disabled');phase.set('CONNECTING SELECTED HANDS')
        except (ValueError,KeyError) as e:messagebox.showerror('Edge settings',str(e))
    def cancel():
        if worker:worker.stop.set();phase.set('STOPPING — completed clips kept; partial evidence retained')
    def close():
        nonlocal closing
        closing=True;cancel()
    def poll():
        nonlocal worker
        if worker:
            events,latest,_=worker.snapshot()
            for e in events:
                if e['kind']!='finished':
                    text=e.get('text',e['kind']);phase.set(text)
                    if e['kind']!='phase':logs.insert('end',text+'\n');logs.see('end')
            for d,(p,v,t) in latest.items():
                statuses[d].set(f'boot={p.boot_id} seq={p.seq} age={time.monotonic()-t:.2f}s')
                for c,x in zip(CHANNELS,v):values[d,c].set(f'{x:.3f}')
            if not worker.thread.is_alive():
                worker.thread.join();worker=None;start_button.config(state='normal')
                for w,choices,key in inputs:w.config(state='disabled' if key in ('person','session') else 'readonly' if choices else 'normal')
                for status in statuses.values():status.set('Disconnected')
        if closing and worker is None:root.destroy();return
        root.after(100,poll)
    buttons=ttk.Frame(root);buttons.pack()
    start_button=ttk.Button(buttons,text='Connect + record selected case',command=start);start_button.pack(side='left',padx=6)
    ttk.Button(buttons,text='Cancel / disconnect',command=cancel).pack(side='left',padx=6)
    ttk.Button(buttons,text='Close',command=close).pack(side='left',padx=6)
    for w,_,key in inputs:
        if key in ('person','session'):w.config(state='disabled')
    root.protocol('WM_DELETE_WINDOW',close);root.after(100,poll);root.mainloop()

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--person',required=True);p.add_argument('--date',default=date.today().strftime('%Y%m%d'))
    p.add_argument('--gyro-dps',type=int,choices=(1000,2000),default=1000)
    p.add_argument('--campaign',default='repaired_v1');p.add_argument('--output',default='data/hand_raw_flex3/repaired_v1')
    p.add_argument('--edges',default='.comms-local/hand_edges/repaired_v1')
    p.add_argument('--edge-seconds',type=float,default=3)
    p.add_argument('--stage',choices=(*TARGETS,'diagnostic'),default='train')
    p.add_argument('--only',action='store_true',help='open just the selected stage (resume)')
    a=p.parse_args(argv)
    for v in (a.person,a.date,a.campaign):token(v)
    if not 3<=a.edge_seconds<=120:p.error('Edge duration must be 3..120 seconds')
    if a.gyro_dps==2000:
        print('Declared +/-2000 dps profile: verify BOTH gloves serial readback shows range_dps=2000, sensitivity=16.4, register=0x18 before recording.',flush=True)
    if a.only:
        edge_gui(a) if a.stage=='diagnostic' else recording_gui(a);return 0
    stages=list(TARGETS)+['diagnostic']
    for stage in stages[stages.index(a.stage):]:
        args=[sys.executable,'-m','tools.hand_collection','--only','--stage',stage,'--person',a.person,
              '--gyro-dps',str(a.gyro_dps),'--date',a.date,'--campaign',a.campaign,'--output',a.output,'--edges',a.edges,'--edge-seconds',str(a.edge_seconds)]
        print(f'Opening {stage}; stable date/session. Close after completing the stage.',flush=True)
        result=subprocess.run(args)
        if result.returncode:
            print(f'GUI failed (exit {result.returncode}). Recordings retained. Resume with the SAME person/date/campaign and --stage {stage}.',flush=True)
            return result.returncode
    return 0

if __name__=='__main__':raise SystemExit(main())
