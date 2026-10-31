"""Opt-in v5 wide-gyro diagnostic GUI; LEFT, RIGHT or BOTH."""
import tkinter as tk
import argparse

from src.hand_ai_flex3.record_both_gui import App, BothWorker
from src.hand_ai_flex3.wide_ble import connection, RECORDING_CONTRACT, TRANSPORT, transport_for, contract_for


class WideWorker(BothWorker):
    def __init__(self,devices=(1,2),gyro_dps=1000):
        self.gyro_dps=gyro_dps;transport_for(gyro_dps)
        super().__init__(devices,connection_factory=connection)

    def prepare_config(self,config):
        return {d:dict(a,recording_contract=contract_for(self.gyro_dps),
                       acquisition_transport=transport_for(self.gyro_dps)) for d,a in config.items()}

    def validate_rows(self,rows):
        for device,trial in rows.items():
            for _,packet,values in trial:
                if packet.version!=5:raise ValueError('Wide recorder received a different packet version')
                if any(abs(v)>=.9*self.gyro_dps for v in values[3:6]):
                    import csv,json,time,hashlib
                    from pathlib import Path
                    from src.hand_ai_flex3.core import CHANNELS
                    dest=Path('.comms-local/rejected_gyro')/str(time.time_ns());dest.mkdir(parents=True)
                    # Capture every selected hand from this rejected trial, outside the training root.
                    for hand,rr in rows.items():
                        with (dest/f'hand_{hand}.csv').open('x',newline='') as f:
                            w=csv.writer(f);w.writerow(['elapsed_s','device_id','boot_id','seq','uptime_ms',*CHANNELS])
                            for elapsed,p,v in rr:w.writerow([elapsed,p.device_id,p.boot_id,p.seq,p.uptime_ms,*v])
                    peaks={str(d):dict(zip(('gx','gy','gz'),[max(abs(v[i]) for _,p,v in rr) for i in (3,4,5)])) for d,rr in rows.items()}
                    (dest/'diagnostic.json').write_text(json.dumps(dict(reason='sensor_headroom_review',range_dps=self.gyro_dps,peaks=peaks,sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.glob('*.csv')}),indent=2))
                    raise ValueError(f'Hand {device}: gyro >= {self.gyro_dps*.9:g} dps. Rejected trial diagnostics retained: {dest}; peaks={peaks}')
                if any(v in (-32768,32767) for v in packet.values):
                    raise ValueError(f'Hand {device}: a channel reached a wire endpoint. Trial not saved; review sensor range/scaling.')


class WideApp(App):
    worker_class=WideWorker

    def __init__(self,root,gyro_dps=1000):
        self.worker_class=lambda devices:WideWorker(devices,gyro_dps=gyro_dps)
        super().__init__(root)
        root.title(f'V5 wide gyro — ±{gyro_dps} dps — Three-Flex Recorder')
        self.phase.set('V5 WIDE GYRO — matching wide firmware required')
        self.vars['session'].set('P01_gyro_wide_check_20261028')
        self.vars['left'].set('WASH');self.vars['right'].set('WASH')
        self.vars['output'].set('data/hand_flex3_checks/wide_gyro')
        self.controls()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--person');p.add_argument('--session');p.add_argument('--split',choices=['train','validation','test'])
    p.add_argument('--gyro-dps',type=int,choices=(1000,2000),default=1000)
    p.add_argument('--output');p.add_argument('--count',type=int)
    a=p.parse_args()
    if a.count is not None and a.count<1:p.error('Count must be positive')
    root=tk.Tk();app=WideApp(root,a.gyro_dps)
    for name,value in vars(a).items():
        if name!='gyro_dps' and value is not None:app.vars[name].set(str(value))
    root.mainloop()


if __name__=='__main__':main()
