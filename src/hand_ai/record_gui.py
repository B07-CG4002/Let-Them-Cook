"""Single-hand BLE recorder with the layout and countdown of the legacy USB GUI.

Run from the repository root: python -m src.hand_ai.record_gui
Sensor transport is BLE. USB may provide power and diagnostic logs.
"""
import asyncio
from collections import deque
import csv
from datetime import datetime, timezone
from pathlib import Path
import queue as thread_queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import numpy as np
from src.hand_ai.core import CHANNELS, LABELS, SPLITS, resample, digest, write_json
from src.hand_ai.ble import connection, NotificationQueue, collect, ensure_healthy

WAIT_SECONDS = 3

def settings(person, session, split, hand, gesture, count, output):
    person, session = person.strip(), session.strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', person) or not re.fullmatch(r'[A-Za-z0-9_-]+', session):
        raise ValueError('Person/session: use letters, digits, underscore or hyphen.')
    if split not in SPLITS or gesture not in LABELS or hand not in ('RIGHT','LEFT'):
        raise ValueError('Select a hand, split and gesture.')
    try: count = int(count)
    except ValueError: raise ValueError('Recording count must be a positive integer.')
    if count < 1: raise ValueError('Recording count must be positive.')
    if not output.strip(): raise ValueError('Enter an output root.')
    device = 2 if hand == 'RIGHT' else 1
    root = Path(output.strip()) / hand.lower() / session
    return dict(person=person, session=session, split=split, device_id=device,
                gesture=gesture, count=count, root=root)


def prepare_session(a):
    import json
    root = a['root']
    root.mkdir(parents=True, exist_ok=True)
    identity = {k:a[k] for k in ('person','session','split','device_id')}
    marker = root/'session.json'
    if marker.exists():
        if json.loads(marker.read_text(encoding='utf-8')) != identity:
            raise ValueError('This folder already belongs to a different person/hand/session/split. Choose a new session.')
    else: write_json(marker,identity)
    prefix = f"{a['person']}_{a['session']}_{a['gesture']}_"
    used = set()
    for p in root.glob(prefix+'*'):
        tail = p.stem[len(prefix):]
        if p.suffix in ('.csv','.json') and tail.isdigit(): used.add(int(tail))
    start = max(used, default=0)+1
    return list(range(start,start+a['count']))


def save_trial(a, trial, rows):
    t = [r[0] for r in rows]
    values = np.asarray([r[2] for r in rows])
    resample(t,values)
    stem = f"{a['person']}_{a['session']}_{a['gesture']}_{trial:03d}"
    path = a['root']/(stem+'.csv')
    sidecar = path.with_suffix('.json')
    if path.exists() or sidecar.exists(): raise FileExistsError(path)
    with path.open('x',encoding='utf-8',newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['elapsed_s','device_id','boot_id','seq','uptime_ms',*CHANNELS])
        for elapsed,p,v in rows: writer.writerow([elapsed,p.device_id,p.boot_id,p.seq,p.uptime_ms,*v])
    identity = {k:a[k] for k in ('person','session','split','device_id','gesture')}
    write_json(sidecar,dict(**identity,trial=trial,csv_sha256=digest(path),
               created_utc=datetime.now(timezone.utc).isoformat()))
    saturated = sum(any(v in (-32768,32767) for v in p.values) for _,p,_ in rows)
    return path, len(rows), t[-1], saturated


class Worker:
    """BLE/asyncio live on a background thread; Tk is touched only by the UI."""
    def __init__(self,device):
        self.device=device; self.commands=thread_queue.Queue(); self.stop=threading.Event()
        self.cancel_recording=threading.Event()
        self.lock=threading.Lock();self.events=deque(maxlen=256);self.latest=None
        self.count=0;self.thread=threading.Thread(target=self.run,daemon=True)
    def event(self,kind,**fields):
        with self.lock:self.events.append(dict(kind=kind,**fields))
    def snapshot(self):
        with self.lock:
            events=list(self.events);self.events.clear()
            return events,self.latest,self.count
    def run(self):
        try:asyncio.run(self.supervise())
        except Exception as error:self.event('error',text=f'{type(error).__name__}: {error}')
        finally:self.event('finished')
    async def supervise(self):
        task=asyncio.create_task(self.session())
        try:
            while not task.done():
                if self.stop.is_set():
                    task.cancel()
                    break
                await asyncio.sleep(.02)
            try:await task
            except asyncio.CancelledError:
                if not self.stop.is_set():raise
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
    async def session(self):
        async with connection(self.device) as (_,incoming,health):
            trial_queue=NotificationQueue(self.device,0,health,trial_mode=True)
            # Trial queue receives already-validated decoded packets, not raw bytes.
            async def receive():
                while True:
                    p,v,received=await asyncio.wait_for(incoming.get(),3)
                    ensure_healthy(health)
                    with self.lock:
                        self.latest=(p,v,received);self.count+=1
                    if trial_queue.accepting and received>=trial_queue.cue:
                        try:trial_queue.put_nowait((p,v,received))
                        except asyncio.QueueFull:health['dropped']+=1
            reader=asyncio.create_task(receive())
            self.event('connected')
            try:
                while not self.stop.is_set():
                    if reader.done():await reader
                    try:command=self.commands.get_nowait()
                    except thread_queue.Empty:
                        await asyncio.sleep(.02);continue
                    if command['kind']=='record':
                        batch=asyncio.create_task(self.batch(command['settings'],trial_queue,health))
                        try:
                            while not batch.done():
                                if self.stop.is_set() or self.cancel_recording.is_set():
                                    batch.cancel();break
                                if reader.done():
                                    batch.cancel();await reader
                                await asyncio.sleep(.02)
                            try:await batch
                            except asyncio.CancelledError:
                                if self.cancel_recording.is_set() and not self.stop.is_set():
                                    self.event('cancelled',text='Batch cancelled. Completed recordings kept; current incomplete trial discarded.')
                        finally:
                            if not batch.done():
                                batch.cancel()
                                await asyncio.gather(batch,return_exceptions=True)
            finally:
                reader.cancel();await asyncio.gather(reader,return_exceptions=True)
    async def batch(self,a,trial_queue,health):
        if self.cancel_recording.is_set():raise asyncio.CancelledError()
        trials=prepare_session(a)
        for i,trial in enumerate(trials,1):
            for remaining in range(WAIT_SECONDS,0,-1):
                self.event('phase',phase='WAIT',text=f'{a["gesture"]} — trial {trial} ({i}/{len(trials)}) starts in {remaining}')
                await asyncio.sleep(1)
            self.event('phase',phase=a['gesture'],text=f'GO — recording trial {trial} for 2 seconds')
            rows=await collect(trial_queue,health)
            path,n,duration,saturated=save_trial(a,trial,rows)
            self.event('saved',text=f'Saved {path} — {n} samples, {duration:.2f}s'+
                       (f' | WARNING: {saturated} samples at transport limits' if saturated else ''))
        self.event('complete',text=f'Saved {len(trials)} {a["gesture"]} recording(s).')


class App:
    def __init__(self,root):
        self.root=root;self.worker=None;self.connected=False;self.busy=False;self.closing=False
        self.previous_count=0;self.previous_time=time.monotonic();self.recent=deque(maxlen=5);self.last_seq=None
        root.title('Independent-Hand BLE Sample Recorder');root.geometry('1300x820');root.minsize(900,700)
        self.phase=tk.StringVar(value='READY');self.countdown=tk.StringVar(value='Connect a glove to begin')
        self.status=tk.StringVar(value='BLE carries sensor data. USB may supply power and serial logs.')
        tk.Label(root,textvariable=self.phase,font=('Segoe UI',28,'bold')).pack(pady=(12,0))
        tk.Label(root,textvariable=self.countdown,font=('Segoe UI',21)).pack(pady=5)
        tk.Label(root,textvariable=self.status,wraplength=1200,font=('Segoe UI',10)).pack(pady=5)
        form=ttk.Frame(root,padding=8);form.pack()
        self.vars={k:tk.StringVar(value=v) for k,v in dict(hand='RIGHT',person='P01',session='pilot1',
                   split='train',gesture='PICKUP',count='3',output='data/hand_pilot').items()}
        self.inputs=[]
        fields=[('Hand','hand',('RIGHT','LEFT')),('Person ID','person',None),('Session','session',None),
                ('Dataset split','split',tuple(SPLITS)),('Gesture','gesture',tuple(LABELS)),
                ('Number of recordings','count',None),('Output root','output',None)]
        for row,(label,key,choices) in enumerate(fields):
            ttk.Label(form,text=label+':').grid(row=row,column=0,sticky='e',padx=8,pady=3)
            widget=ttk.Combobox(form,textvariable=self.vars[key],values=choices,state='readonly',width=44) if choices else ttk.Entry(form,textvariable=self.vars[key],width=47)
            widget.grid(row=row,column=1,sticky='w',padx=8,pady=3);self.inputs.append((widget,choices))
        ttk.Label(form,text='Recording: 2 seconds per trial; preparation: 3 seconds.').grid(row=7,columnspan=2,pady=5)
        self.preview=tk.StringVar();ttk.Label(form,textvariable=self.preview,wraplength=1000).grid(row=8,columnspan=2)
        for v in self.vars.values():v.trace_add('write',self.preview_path)
        self.preview_path()
        buttons=ttk.Frame(root);buttons.pack(pady=8)
        self.connect_button=ttk.Button(buttons,text='Connect BLE',command=self.connect);self.connect_button.pack(side='left',padx=8)
        self.start_button=ttk.Button(buttons,text='Start Recording',command=self.start,state='disabled');self.start_button.pack(side='left',padx=8)
        self.cancel_button=ttk.Button(buttons,text='Cancel Recording',command=self.cancel,state='disabled');self.cancel_button.pack(side='left',padx=8)
        self.disconnect_button=ttk.Button(buttons,text='Disconnect BLE',command=self.disconnect,state='disabled');self.disconnect_button.pack(side='left',padx=8)
        self.live=tk.StringVar(value='Waiting for sensor data')
        ttk.Label(root,textvariable=self.live,font=('Segoe UI',11,'bold')).pack(pady=5)
        table=ttk.Frame(root);table.pack(fill='x',padx=25)
        self.values={}
        for i,c in enumerate(CHANNELS):
            col=i%6;row=(i//6)*2
            ttk.Label(table,text=c).grid(row=row,column=col,padx=15,pady=3)
            value=tk.StringVar(value='—');self.values[c]=value
            ttk.Label(table,textvariable=value,font=('Consolas',13)).grid(row=row+1,column=col,padx=15,pady=3)
        self.logs=tk.Text(root,height=9,wrap='none',state='disabled',font=('Consolas',10));self.logs.pack(fill='both',expand=True,padx=16,pady=10)
        root.protocol('WM_DELETE_WINDOW',self.close);root.after(100,self.poll)
    def preview_path(self,*_):
        v=self.vars
        self.preview.set(str(Path(v['output'].get())/v['hand'].get().lower()/v['session'].get())+
                         f"/{v['person'].get()}_{v['session'].get()}_{v['gesture'].get()}_<trial>.csv + JSON")
    def log(self,text):
        self.logs.configure(state='normal');self.logs.insert('end',text+'\n');self.logs.see('end');self.logs.configure(state='disabled')
    def controls(self):
        for widget,choices in self.inputs:widget.configure(state='disabled' if self.busy else ('readonly' if choices else 'normal'))
        # Hand selection stays fixed for the lifetime of a connection.
        self.inputs[0][0].configure(state='disabled' if self.worker else 'readonly')
        self.connect_button.configure(state='disabled' if self.worker else 'normal')
        stopping=bool(self.worker and self.worker.stop.is_set())
        self.disconnect_button.configure(state='normal' if self.worker and not stopping else 'disabled')
        self.cancel_button.configure(state='normal' if self.busy and self.worker and not stopping and not self.worker.cancel_recording.is_set() else 'disabled')
        self.start_button.configure(state='normal' if self.connected and not self.busy and not stopping else 'disabled')
    def connect(self):
        if self.worker:return
        self.worker=Worker(2 if self.vars['hand'].get()=='RIGHT' else 1)
        self.previous_count=0;self.previous_time=time.monotonic();self.last_seq=None
        self.phase.set('CONNECTING');self.worker.thread.start();self.controls()
    def start(self):
        try:
            v=self.vars;a=settings(*(v[k].get() for k in ('person','session','split','hand','gesture','count','output')))
        except ValueError as e:messagebox.showerror('Invalid recording settings',str(e),parent=self.root);return
        if self.worker and self.connected:
            self.worker.cancel_recording.clear()
            self.busy=True;self.controls();self.worker.commands.put(dict(kind='record',settings=a))
    def cancel(self):
        if self.worker and self.busy:
            self.worker.cancel_recording.set()
            self.status.set('Cancelling current batch; BLE stays connected.')
            self.controls()
    def disconnect(self):
        if self.worker:
            self.worker.stop.set()
            self.phase.set('DISCONNECTING')
            self.countdown.set('Closing Bluetooth connection...')
            self.status.set('Completed recordings are kept. Incomplete current trial is discarded.')
        self.connected=False;self.controls()
    def close(self):
        if self.closing:return
        self.closing=True
        self.disconnect()
        self.finish_close()
    def finish_close(self):
        if self.worker and self.worker.thread.is_alive():
            self.root.after(100,self.finish_close)
        else:self.root.destroy()
    def poll(self):
        if self.closing:return
        if self.worker:
            events,latest,count=self.worker.snapshot()
            for e in events:
                kind=e['kind']
                if kind=='connected':
                    if not self.worker.stop.is_set():
                        self.connected=True;self.phase.set('READY');self.countdown.set('Press Start Recording')
                elif kind=='phase':
                    if not self.worker.stop.is_set():self.phase.set(e['phase']);self.countdown.set(e['text'])
                elif kind=='saved':self.log(e['text']);self.status.set(e['text'])
                elif kind in ('complete','cancelled'):
                    self.busy=False
                    if not self.worker.stop.is_set():
                        self.phase.set('CANCELLED' if kind=='cancelled' else 'FINISHED')
                        self.countdown.set('Press Start Recording to begin a new batch')
                    self.status.set(e['text']);self.log(e['text'])
                elif kind=='error':self.phase.set('ERROR');self.status.set(e['text']);self.log(e['text'])
                elif kind=='finished':
                    self.connected=False;self.busy=False;self.worker=None
                    if self.phase.get()!='ERROR':
                        self.phase.set('DISCONNECTED');self.countdown.set('Select a hand and connect again')
                        self.status.set('Recorder connection closed.')
                    self.live.set('Disconnected')
            if latest and self.worker and self.connected:
                p,v,received=latest
                for c,value in zip(CHANNELS,v):self.values[c].set(f'{value:.4f}')
                now=time.monotonic();dt=now-self.previous_time
                if dt>=1:
                    rate=(count-self.previous_count)/dt;self.previous_count=count;self.previous_time=now
                    self.live.set(f'{"RIGHT" if p.device_id==2 else "LEFT"} | {rate:.1f} samples/s | boot={p.boot_id} | seq={p.seq}')
                if now-received>1:self.live.set('Sensor data stale — check glove connection')
            self.controls()
        self.root.after(100,self.poll)


def main():
    root=tk.Tk();App(root);root.mainloop()

if __name__=='__main__':main()
