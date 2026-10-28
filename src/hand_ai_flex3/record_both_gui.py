"""One GO cue, two BLE gloves, separate source-timestamped native trial files."""
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
from tkinter import ttk, messagebox
import uuid
import numpy as np

from src.hand_ai_flex3.record_gui import Worker, settings, prepare_session, save_trial
from src.hand_ai_flex3.ble import connection, NotificationQueue, collect, ensure_healthy
from src.hand_ai_flex3.core import CHANNELS, LABELS, SPLITS

WAIT_SECONDS = 2


class SessionConflict(ValueError):
    """A recording request conflicts with an existing immutable session."""


def validate_session_settings(config):
    """Check every selected hand without creating or modifying any files."""
    for device, a in config.items():
        marker = a['root'] / 'session.json'
        if not marker.exists():
            continue
        identity = {k: a[k] for k in ('person', 'session', 'split', 'device_id')}
        from src.hand_ai_flex3.core import CONFIG
        identity['recording_contract'] = a.get('recording_contract', CONFIG['version'])
        try:
            existing = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise SessionConflict(f'Cannot read session marker {marker}: {error}. Existing recordings were kept.') from error
        if existing != identity:
            saved_split = existing.get('split', 'unknown') if isinstance(existing, dict) else 'unknown'
            raise SessionConflict(
                f'Hand {device}: session "{a["session"]}" already belongs to split "{saved_split}". '
                f'The selected split is "{a["split"]}". Use a separate session name for each split, '
                'or restore the original session settings to continue recording. '
                f'Existing recordings were kept. Folder: {a["root"]}')


def pair_settings(person, session, split, left, right, count, output):
    return {d:settings(person,session,split,hand,g,count,output)
            for d,hand,g in ((1,'LEFT',left),(2,'RIGHT',right))}


def save_pair(config, trials, rows):
    """Stage selected hands first; roll back only newly created files on failure."""
    devices=tuple(sorted(config))
    if devices not in ((1,),(2,),(1,2)):raise ValueError('Select LEFT, RIGHT or BOTH')
    for d in devices:
        values=rows[d]
        if len(values)<85 or not 1.85<=values[-1][0]<=2.1:raise ValueError('Incomplete hand trial')
        packets=[r[1] for r in values]
        if any(p.device_id!=d or p.boot_id!=packets[0].boot_id for p in packets):raise ValueError('Wrong source identity')
        dt=np.array([(b.uptime_ms-a.uptime_ms)%2**32 for a,b in zip(packets,packets[1:])])
        if (np.any(dt<=0) or np.any(dt>100) or
                any((b.seq-a.seq)%2**32!=1 for a,b in zip(packets,packets[1:])) or
                not np.allclose([r[0] for r in values],np.r_[0.,np.cumsum(dt)/1000.],atol=1e-6)):
            raise ValueError('Source timing/sequence gap')
    capture_id=uuid.uuid4().hex
    targets=[];created=[]
    for d in devices:
        a=config[d];stem=f'{a["person"]}_{a["session"]}_{a["gesture"]}_{trials[d]:03d}'
        targets.extend(a['root']/(stem+s) for s in ('.csv','.json'))
    if any(p.exists() for p in targets):raise FileExistsError('Pair output already exists')
    # Stage on the same filesystem as the output, for exclusive hard-link commit.
    parent=config[devices[0]]['root'].parent
    with tempfile.TemporaryDirectory(prefix='.pair-stage-',dir=parent) as temp:
        staged=[]
        for d in devices:
            a=dict(config[d],root=Path(temp)/str(d));a['root'].mkdir()
            path,*_=save_trial(a,trials[d],rows[d])
            meta=json.loads(path.with_suffix('.json').read_text())
            meta.update(capture_id=capture_id,captured_devices=list(devices),
                        acquisition='shared host GO; independent source clocks, not sample-aligned')
            if len(devices)==2:
                meta.update(pair_capture_id=capture_id,other_device_id=3-d,other_gesture=config[3-d]['gesture'])
            if 'acquisition_transport' in config[d]:
                meta['acquisition_transport']=config[d]['acquisition_transport']
            path.with_suffix('.json').write_text(json.dumps(meta,indent=2)+'\n')
            staged.extend((path,path.with_suffix('.json')))
        try:
            for source,target in zip(staged,targets):
                os.link(source,target)  # exclusive: never replace an existing trial
                created.append(target)
        except BaseException:
            for target in reversed(created):target.unlink()
            raise
    return capture_id


class BothWorker(Worker):
    def __init__(self,devices=(1,2),*,connection_factory=None):
        if any(type(d) is not int for d in devices) or tuple(devices) not in ((1,),(2,),(1,2)):raise ValueError('Invalid selected hands')
        self.devices=tuple(devices)
        self.connection_factory=connection_factory
        super().__init__(0);self.latest={};self.links={};self.trials={}

    def snapshot(self):
        with self.lock:
            events=list(self.events);self.events.clear()
            return events,dict(self.latest),self.count

    async def session(self):
        async with AsyncExitStack() as stack:
            for d in self.devices:
                self.event('hand_state',device=d,state='Connecting')
                factory=self.connection_factory or connection
                _,incoming,health=await stack.enter_async_context(factory(d))
                self.event('hand_state',device=d,state='Connected')
                self.links[d]=(incoming,health)
                self.trials[d]=NotificationQueue(d,0,health,trial_mode=True)
            async def receive(d):
                incoming,health=self.links[d];trial=self.trials[d]
                while True:
                    p,v,received=await asyncio.wait_for(incoming.get(),3)
                    ensure_healthy(health)
                    with self.lock:self.latest[d]=(p,v,received);self.count+=1
                    if trial.accepting and received>=trial.cue:
                        try:trial.put_nowait((p,v,received))
                        except asyncio.QueueFull:health['dropped']+=1
            readers=[asyncio.create_task(receive(d)) for d in self.devices]
            self.event('connected')
            try:
                while True:
                    for reader in readers:
                        if reader.done():await reader
                    try:command=self.commands.get_nowait()
                    except Exception as error:
                        import queue
                        if not isinstance(error,queue.Empty):raise
                        await asyncio.sleep(.02);continue
                    task=asyncio.create_task(self.batch_both(command['settings']))
                    try:
                        while not task.done():
                            if self.cancel_recording.is_set():task.cancel();break
                            for reader in readers:
                                if reader.done():await reader
                            await asyncio.sleep(.02)
                        try:await task
                        except asyncio.CancelledError:
                            if not self.cancel_recording.is_set():raise
                            self.event('cancelled',text='Cancelled; completed pairs retained, current pair discarded.')
                    except SessionConflict as error:
                        self.event('batch_error',text=str(error))
                    finally:
                        if not task.done():task.cancel()
                        await asyncio.gather(task,return_exceptions=True)
            finally:
                for task in readers:task.cancel()
                await asyncio.gather(*readers,return_exceptions=True)

    def prepare_config(self,config):
        return config

    async def batch_both(self,config):
        config=self.prepare_config(config)
        validate_session_settings(config)
        numbers={d:prepare_session(config[d]) for d in self.devices}
        for i in range(config[self.devices[0]]['count']):
            for second in range(WAIT_SECONDS,0,-1):
                self.event('phase',text=f'WAIT {second} — '+ ' / '.join(f'{d}:{config[d]["gesture"]}' for d in self.devices))
                await asyncio.sleep(1)
            self.event('phase',text=f'GO — capture {i+1}/{config[self.devices[0]]["count"]}, action/hold for 2 seconds')
            tasks={d:asyncio.create_task(collect(self.trials[d],self.links[d][1])) for d in self.devices}
            try:
                values=await asyncio.gather(*(tasks[d] for d in self.devices))
            finally:
                for task in tasks.values():
                    if not task.done():task.cancel()
                await asyncio.gather(*tasks.values(),return_exceptions=True)
            for d in self.devices:ensure_healthy(self.links[d][1])
            self.validate_rows(dict(zip(self.devices,values)))
            pair=save_pair(config,{d:numbers[d][i] for d in self.devices},dict(zip(self.devices,values)))
            self.event('saved',text='Saved: '+ ' / '.join(f'{d}: #{numbers[d][i]}' for d in self.devices)+f' | capture={pair}')
        self.event('complete',text='Batch complete. Files remain separate for independent-hand training.')

    def validate_rows(self,rows):
        """An opt-in transport may apply additional acquisition checks."""
        return None


class App:
    worker_class=BothWorker
    def __init__(self,root):
        self.root=root;self.worker=None;self.connected=False;self.busy=False;self.closing=False
        root.title('One or Both Gloves — Three-Flex BLE Recorder');root.geometry('1080x750')
        self.phase=tk.StringVar(value='READY — select LEFT only, RIGHT only or BOTH')
        ttk.Label(root,textvariable=self.phase,font=('Segoe UI',20)).pack(pady=15)
        ttk.Label(root,text='Raw 12 channels retained. RIGHT index may be faulty; model exclusion is a separate explicit training option.').pack()
        self.vars={k:tk.StringVar(value=v) for k,v in dict(mode='BOTH',person='P01',session='P01_dual_pilot1',split='train',
                   left='PICKUP',right='PICKUP',count='3',output='data/hand_flex3_checks/dual_pilot').items()}
        form=ttk.Frame(root);form.pack(pady=10);self.inputs=[];self.field_widgets={}
        for i,(k,label,choices) in enumerate([('mode','Capture hands',('LEFT only','RIGHT only','BOTH')),('person','Person',None),('session','Session',None),('split','Split',SPLITS),
                ('left','LEFT action',LABELS),('right','RIGHT action',LABELS),('count','Additional captures',None),('output','Output root',None)]):
            ttk.Label(form,text=label).grid(row=i,column=0,padx=8,pady=3)
            w=ttk.Combobox(form,textvariable=self.vars[k],values=choices,state='readonly',width=55) if choices else ttk.Entry(form,textvariable=self.vars[k],width=58)
            w.grid(row=i,column=1,pady=3);self.inputs.append((w,choices))
            self.field_widgets[k]=w
            if k=='mode':self.mode_widget=w
        ttk.Label(root,text='Use a separate session name for train, validation and test. Changing Split alone does not create a new session.').pack(pady=3)
        buttons=ttk.Frame(root);buttons.pack(pady=8)
        self.buttons={}
        for name,command in [('Connect selected',self.connect),('Record selected',self.start),('Cancel batch',self.cancel),('Disconnect',self.disconnect)]:
            b=ttk.Button(buttons,text=name,command=command);b.pack(side='left',padx=6);self.buttons[name]=b
        ttk.Label(root,text='Preparation: 2 seconds | Capture: 2 seconds | Only selected hands connect and save').pack(pady=4)
        self.live={};self.readings={};self.hand_states={1:'Disconnected',2:'Disconnected'}
        for d,hand in ((1,'LEFT'),(2,'RIGHT')):
            panel=ttk.LabelFrame(root,text=hand,padding=6)
            panel.pack(fill='x',padx=12,pady=5)
            self.live[d]=tk.StringVar(value='Disconnected')
            ttk.Label(panel,textvariable=self.live[d],width=80,anchor='w',font=('Consolas',10)).grid(
                row=0,column=0,columnspan=6,sticky='w')
            for col in range(6):panel.columnconfigure(col,weight=1,uniform='sensor',minsize=110)
            for i,channel in enumerate(CHANNELS):
                row=1+2*(i//6);col=i%6
                ttk.Label(panel,text=channel,anchor='center').grid(row=row,column=col,sticky='ew')
                value=tk.StringVar(value='—');self.readings[d,channel]=value
                ttk.Label(panel,textvariable=value,width=11,anchor='e',font=('Consolas',11)).grid(
                    row=row+1,column=col,sticky='ew',padx=8)
        self.logbox=tk.Text(root,height=10);self.logbox.pack(fill='both',expand=True,padx=12,pady=8)
        root.protocol('WM_DELETE_WINDOW',self.close);self.controls();root.after(100,self.poll)

    def controls(self):
        states={'Connect selected':not self.worker,'Record selected':self.connected and not self.busy,
                'Cancel batch':self.busy,'Disconnect':bool(self.worker)}
        for k,b in self.buttons.items():b.configure(state='normal' if states[k] else 'disabled')
        for w,choices in self.inputs:w.configure(state='disabled' if self.busy else ('readonly' if choices else 'normal'))
        self.mode_widget.configure(state='disabled' if self.worker else 'readonly')
        selected={'LEFT only':(1,),'RIGHT only':(2,),'BOTH':(1,2)}[self.vars['mode'].get()]
        for d,key in ((1,'left'),(2,'right')):
            self.field_widgets[key].configure(state='readonly' if d in selected and not self.busy else 'disabled')
    def connect(self):
        if self.worker:return
        devices={'LEFT only':(1,),'RIGHT only':(2,),'BOTH':(1,2)}[self.vars['mode'].get()]
        self.hand_states={d:('Connecting' if d in devices else 'Disconnected') for d in (1,2)}
        self.worker=self.worker_class(devices);self.phase.set('CONNECTING SELECTED HANDS');self.worker.thread.start();self.controls()
    def start(self):
        try:
            v={k:v.get() for k,v in self.vars.items()}
            config=pair_settings(v['person'],v['session'],v['split'],v['left'],v['right'],v['count'],v['output'])
            config=self.worker.prepare_config({d:config[d] for d in self.worker.devices})
            validate_session_settings(config)
        except (ValueError,OSError) as error:messagebox.showerror('Recording settings',str(error));return
        self.worker.cancel_recording.clear();self.busy=True
        self.worker.commands.put(dict(settings=config));self.controls()
    def cancel(self):
        if self.worker:self.worker.cancel_recording.set()
    def disconnect(self):
        if self.worker:self.worker.stop.set()
        self.connected=False;self.phase.set('DISCONNECTING');self.controls()
    def close(self):self.closing=True;self.disconnect()
    def poll(self):
        if self.worker:
            events,latest,_=self.worker.snapshot()
            for event in events:
                kind=event['kind'];text=event.get('text',kind)
                if kind=='hand_state':self.hand_states[event['device']]=event['state']
                if kind=='connected' and not self.worker.stop.is_set():self.connected=True
                if kind in ('complete','cancelled','batch_error'):self.busy=False
                if kind=='finished':self.worker=None;self.connected=False;self.busy=False
                self.phase.set(text)
                if kind!='phase':self.logbox.insert('end',text+'\n');self.logbox.see('end')
            for d,(p,values,received) in latest.items():
                if self.worker and self.connected:
                    self.live[d].set(f'seq={p.seq:10d}   age={time.monotonic()-received:6.2f}s')
                    for channel,value in zip(CHANNELS,values):self.readings[d,channel].set(f'{value:10.3f}')
        if not self.worker:
            for d in (1,2):
                self.live[d].set('Disconnected')
                for channel in CHANNELS:self.readings[d,channel].set('—')
        for d in (1,2):
            if not self.worker or d not in self.worker.devices:
                self.live[d].set('Disconnected')
                for channel in CHANNELS:self.readings[d,channel].set('—')
            elif not self.connected:
                self.live[d].set('Disconnecting' if self.worker.stop.is_set() else self.hand_states[d])
        self.controls()
        if self.closing and not self.worker:self.root.destroy();return
        self.root.after(100,self.poll)


def main():
    root=tk.Tk();App(root);root.mainloop()


if __name__=='__main__':main()
