"""Guided paired-hand collection. New campaigns only; existing data stay unchanged."""
import argparse
from datetime import date
import json
from pathlib import Path
from tools.hand_capture_review import inventory, quarantine
from tools.hand_collection import session_name

# Wording and allocations agreed with Wen Ree, including swapped THROW endpoints.
VARIANTS = [
('PICKUP',5,'underhand supporting lift; thumb straight'),
('PICKUP',5,'underhand supporting lift; thumb bent'),
('PICKUP',10,'overhand grasp and lift; thumb straight'),
('PICKUP',10,'overhand grasp and lift; thumb bent'),
('DROP',8,'elbows bent; stationary release; thumb straight'),
('DROP',7,'elbows bent; stationary release; thumb bent'),
('DROP',7,'arms forward; stationary release; thumb straight'),
('DROP',8,'arms forward; stationary release; thumb bent'),
('CHOP',8,'normal speed; lift then one downstroke; thumb straight'),
('CHOP',7,'normal speed; lift then one downstroke; thumb bent'),
('CHOP',7,'faster speed; lift then one downstroke; thumb straight'),
('CHOP',8,'faster speed; lift then one downstroke; thumb bent'),
('WASH',8,'left hand above; arms forward; open grip; one circle'),
('WASH',7,'left hand above; elbows tucked; open grip; one circle'),
('WASH',7,'right hand above; arms forward; open grip; one circle'),
('WASH',8,'right hand above; elbows tucked; open grip; one circle'),
('THROW',8,'chest-forward thrust and release; starting thumb straight; finish palm vertical'),
('THROW',7,'chest-forward thrust and release; starting thumb straight; finish palm horizontal'),
('THROW',7,'chest-forward thrust and release; starting thumb bent; finish palm vertical'),
('THROW',8,'chest-forward thrust and release; starting thumb bent; finish palm horizontal'),
('POURING',8,'arms forward; tilt towards body and hold; thumb straight'),
('POURING',7,'arms forward; tilt towards body and hold; thumb bent'),
('POURING',7,'elbows tucked; tilt towards body and hold; thumb straight'),
('POURING',8,'elbows tucked; tilt towards body and hold; thumb bent'),
('IDLE',6,'hands by side; open hands'),('IDLE',4,'hands by side; closed grip; thumb straight'),
('IDLE',4,'hands by side; closed grip; thumb bent'),('IDLE',6,'hands resting on table; open hands'),
('IDLE',4,'hands resting on table; closed grip; thumb straight'),('IDLE',4,'hands resting on table; closed grip; thumb bent'),
('IDLE',1,'hands by side; small finger movements'),('IDLE',1,'hands on table; light finger tapping')]

def allocation(weights, total):
    # Largest remainder; stable ties. Counts sum exactly to chosen budget.
    base=[w*total//sum(weights) for w in weights]
    order=sorted(range(len(weights)),key=lambda i: (-(weights[i]*total%sum(weights)),i))
    for i in order[:total-sum(base)]: base[i]+=1
    return base

def plan(train_target):
    if train_target not in (20,30): raise ValueError('Choose 20 (20/3/4) or 30 (30/4/6); other budgets need a reviewed split plan')
    rows=[]
    for split in ('train','validation','test'):
        for gesture in dict.fromkeys(v[0] for v in VARIANTS):
            variants=[v for v in VARIANTS if v[0]==gesture]
            if split=='train':
                counts=([v[1] for v in variants] if train_target==30 else [1+x for x in allocation([v[1]-1 for v in variants],train_target-len(variants))])
            elif gesture=='IDLE': counts=[1,1,0,1,0,1,0,0] if split=='validation' else [1,1,1,1,1,1,0,0]
            else: counts=([1]*4 if split=='validation' else ([1,1,2,2] if gesture=='PICKUP' else [2,1,1,2]))
            if train_target==20 and split=='validation':
                counts=([1,1,0,0,0,1,0,0] if gesture=='IDLE' else [1,1,1,0])
            if train_target==20 and split=='test':
                counts=([1,1,0,1,0,1,0,0] if gesture=='IDLE' else [1,1,1,1])
            rows.extend(dict(split=split,gesture=g,note=g+'; '+n,target=c) for (g,w,n),c in zip(variants,counts) if c)
    return rows

def counts_for(rows, row, person, session):
    return {d:sum(1 for r in rows if r['meta']['person']==person and r['meta']['session']==session and
                  r['meta']['device_id']==d and r['meta']['gesture']==row['gesture'] and
                  r['meta'].get('recording_variant','').strip()==row['note']) for d in (1,2)}

def launch(a):
    import tkinter as tk
    from tkinter import ttk,messagebox,simpledialog
    from src.hand_ai_flex3.wide_record_gui import WideApp
    from src.hand_ai_flex3.record_both_gui import pair_settings,validate_session_settings
    rows=plan(a.train_target)
    rootpath=Path('data/hand_raw_flex3')/a.campaign
    identity=dict(version=1,person=a.person,date=a.date,campaign=a.campaign,train_target=a.train_target,
                  gyro_dps=2000,rows=rows)
    marker=Path('.comms-local/guided_plans')/a.campaign/(a.person+'_'+a.date+'.json')
    if marker.exists():
        if json.loads(marker.read_text())!=identity: raise ValueError('Plan identity changed. Use original arguments or a new campaign.')
    else:
        # Avoid guessing variants or adopting old/manual data into the guided plan.
        if rootpath.exists():
            if any(r['meta']['person']==a.person for r in inventory(rootpath)):
                raise ValueError('Existing wearer data have no guided plan. Keep using legacy resume; use a NEW campaign for guided collection.')
        marker.parent.mkdir(parents=True,exist_ok=True)
        with marker.open('x') as f: json.dump(identity,f,indent=2)
    class App(WideApp):
        def __init__(self,root):
            self.ready=False;self.index=0;self.records=[]
            super().__init__(root,gyro_dps=2000)
            root.title('Guided v5 collection — BOTH hands — +/-2000 dps');root.geometry('1180x1000')
            self.vars['mode'].set('BOTH')
            form=self.field_widgets['output'].master;row=form.grid_size()[1]
            self.note=tk.StringVar();self.progress=tk.StringVar()
            ttk.Label(form,text='Automatic batch description').grid(row=row,column=0)
            ttk.Entry(form,textvariable=self.note,state='readonly',width=100).grid(row=row,column=1)
            ttk.Label(form,textvariable=self.progress).grid(row=row+1,column=0,columnspan=2)
            ttk.Label(form,text='Palm horizontal = flat, palm facing floor/ceiling. Vertical = edge-on, palm facing sideways.\nKeep that agreed meaning consistent; confirm with a reference photo. Charge gloves during breaks.\nDROP: hold arm approximately still and open. THROW: propel forward then release.').grid(row=row+2,column=0,columnspan=2)
            bar=ttk.Frame(form);bar.grid(row=row+3,column=0,columnspan=2)
            self.nav=[]
            for label,fn in [('Previous',lambda:self.select(-1)),('Next',lambda:self.select(1)),('Next split',self.next_split),('Review / redo capture',self.review)]:
                w=ttk.Button(bar,text=label,command=fn);w.pack(side='left',padx=6);self.nav.append(w)
            self.logbox.configure(height=4)
            # One action selector is shown as a read-only mirror; both are filled from plan.
            self.field_widgets['right'].grid_remove()
            ttk.Label(form,text='Same gesture as LEFT (locked)').grid(row=5,column=1,sticky='w')
            self.ready=True;self.reload();self.first_incomplete(a.stage);self.show()
        def controls(self):
            super().controls()
            if not self.ready:return
            for w,choices in self.inputs:w.configure(state='disabled')
            for w in self.nav:w.configure(state='disabled' if self.busy else 'normal')
        def reload(self):
            self.records=inventory(rootpath) if rootpath.exists() else []
        def session(self,row):return session_name(a.person,a.date,a.campaign,row['split'])
        def counts(self,row):return counts_for(self.records,row,a.person,self.session(row))
        def first_incomplete(self,split):
            candidates=[i for i,r in enumerate(rows) if r['split']==split and min(self.counts(r).values())<r['target']]
            self.index=candidates[0] if candidates else next(i for i,r in enumerate(rows) if r['split']==split)
        def show(self):
            r=rows[self.index];c=self.counts(r)
            for k,v in dict(person=a.person,session=self.session(r),split=r['split'],left=r['gesture'],right=r['gesture'],
                            output=str(rootpath),count=max(0,min(a.batch,r['target']-max(c.values())))).items():self.vars[k].set(str(v))
            self.note.set(r['note']);self.progress.set(f'Plan row {self.index+1}/{len(rows)} | LEFT {c[1]}/{r["target"]} RIGHT {c[2]}/{r["target"]} | {r["split"]}')
            self.controls()
        def select(self,delta):
            if self.busy:return
            j=max(0,min(len(rows)-1,self.index+delta))
            if rows[j]['split']!=rows[self.index]['split']:
                if not messagebox.askokcancel('Separate capture session','Take a break, refit gloves and confirm palm/grip references.\nContinue to the selected split?'):return
            self.index=j;self.show()
        def next_split(self):
            if self.busy:return
            self.reload();split=rows[self.index]['split']
            if any(min(self.counts(r).values())<r['target'] for r in rows if r['split']==split):
                messagebox.showerror('Incomplete split','Finish or correct the remaining variants in this split first.');return
            splits=('train','validation','test');n=splits.index(split)+1
            if n==len(splits):
                messagebox.showinfo('Collection complete','All three splits complete. Close and run Audit. Edge recordings are optional and separate.');return
            if messagebox.askokcancel('Break and refit','Take a break, refit the gloves and check grip/palm references before the next session.'):
                self.first_incomplete(splits[n]);self.show()
        def start(self):
            if not self.worker or not self.connected:return
            try:
                self.reload();r=rows[self.index];c=self.counts(r)
                if c[1]!=c[2]:raise ValueError('Unequal hand counts; review paired data before continuing.')
                self.before_count=c[1]
                n=min(a.batch,r['target']-c[1])
                if n<=0:raise ValueError('This variant is complete. Use Next or Review / redo capture.')
                v={k:x.get() for k,x in self.vars.items()}
                config=pair_settings(a.person,self.session(r),r['split'],r['gesture'],r['gesture'],str(n),str(rootpath))
                config=self.worker.prepare_config(config);validate_session_settings(config)
                for x in config.values():x['recording_variant']=r['note']
                self.worker.cancel_recording.clear();self.busy=True
                self.worker.commands.put(dict(settings=config));self.controls()
            except (ValueError,OSError) as e:messagebox.showerror('Recording settings',str(e))
        def poll(self):
            if self.closing:return
            was_busy=self.busy
            super().poll()
            if self.ready and was_busy and not self.busy:
                try:
                    self.reload();r=rows[self.index]
                    # Advance only within the split. No automatic recording starts.
                    if min(self.counts(r).values())>getattr(self,'before_count',-1):
                        same=[i for i,x in enumerate(rows) if x['split']==r['split'] and min(self.counts(x).values())<x['target']]
                        if same:self.index=next((i for i in same if i>self.index),same[0])
                        else:messagebox.showinfo('Split complete','This split is complete. Use Next for the next split after a break, or close. Edge recordings are optional and launched separately.')
                    self.show()
                except (ValueError,OSError) as e:messagebox.showerror('Review required',str(e))
        def review(self):
            if self.busy:return
            try:self.reload()
            except (ValueError,OSError) as e:messagebox.showerror('Review',str(e));return
            win=tk.Toplevel(self.root);win.title('Select a capture to preserve outside the dataset');win.geometry('1100x500')
            tree=ttk.Treeview(win,columns=('time','split','gesture','notes'),show='headings',selectmode='browse')
            for key in ('time','split','gesture','notes'):tree.heading(key,text=key);tree.column(key,width=150 if key!='notes' else 550)
            tree.pack(fill='both',expand=True);seen=set()
            for x in sorted(self.records,key=lambda x:x['meta'].get('created_utc',''),reverse=True):
                m=x['meta'];cap=m['capture_id']
                if m['person']!=a.person or m['session'] not in {self.session(r) for r in rows} or cap in seen:continue
                seen.add(cap);tree.insert('', 'end',iid=cap,values=(m.get('created_utc',''),m['split'],m['gesture'],m.get('recording_variant','')))
            def redo():
                sel=tree.selection()
                if not sel:return
                reason=simpledialog.askstring('Correction reason','What was wrong with this capture?',parent=win)
                if not reason or not reason.strip():return
                if not messagebox.askyesno('Preserve and redo','Preserve BOTH hands of this capture outside the dataset, freeing one trial per hand?',parent=win):return
                try:
                    dest=quarantine(rootpath,sel[0],reason,'.comms-local/backups/capture_review',True)
                    self.reload();self.show();tree.delete(sel[0]);messagebox.showinfo('Preserved',str(dest),parent=win)
                except (ValueError,OSError) as e:messagebox.showerror('Review',str(e),parent=win)
            ttk.Button(win,text='Preserve selected capture for redo',command=redo).pack(pady=8)
    root=tk.Tk();App(root);root.mainloop()

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--person',required=True)
    p.add_argument('--date',default=date.today().strftime('%Y%m%d'));p.add_argument('--campaign',default='repaired_drop_g2000_guided_v2')
    p.add_argument('--train-target',type=int,default=30);p.add_argument('--batch',type=int,default=3)
    p.add_argument('--stage',choices=('train','validation','test'),default='train');p.add_argument('--print-plan',action='store_true')
    a=p.parse_args();session_name(a.person,a.date,a.campaign,a.stage)
    if not 1<=a.batch<=10:p.error('--batch must be 1..10')
    rows=plan(a.train_target)
    if a.print_plan:print(json.dumps(rows,indent=2));return
    launch(a)
if __name__=='__main__':main()
