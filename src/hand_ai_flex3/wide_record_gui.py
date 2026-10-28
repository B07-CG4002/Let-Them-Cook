"""Opt-in v5 wide-gyro diagnostic GUI; LEFT, RIGHT or BOTH."""
import tkinter as tk
import argparse

from src.hand_ai_flex3.record_both_gui import App, BothWorker
from src.hand_ai_flex3.wide_ble import connection, RECORDING_CONTRACT, TRANSPORT


class WideWorker(BothWorker):
    def __init__(self,devices=(1,2)):
        super().__init__(devices,connection_factory=connection)

    def prepare_config(self,config):
        return {d:dict(a,recording_contract=RECORDING_CONTRACT,
                       acquisition_transport=dict(TRANSPORT)) for d,a in config.items()}

    def validate_rows(self,rows):
        for device,trial in rows.items():
            for _,packet,values in trial:
                if packet.version!=5:raise ValueError('Wide recorder received a different packet version')
                if any(abs(v)>=900 for v in values[3:6]):
                    raise ValueError(f'Hand {device}: gyro approaches ±1000 dps sensor range. Trial not saved; review before more collection.')
                if any(v in (-32768,32767) for v in packet.values):
                    raise ValueError(f'Hand {device}: a channel reached a wire endpoint. Trial not saved; review sensor range/scaling.')


class WideApp(App):
    worker_class=WideWorker

    def __init__(self,root):
        super().__init__(root)
        root.title('V5 wide gyro — ±1000 dps — Three-Flex Recorder')
        self.phase.set('V5 WIDE GYRO — matching wide firmware required')
        self.vars['session'].set('P01_gyro_wide_check_20261028')
        self.vars['left'].set('WASH');self.vars['right'].set('WASH')
        self.vars['output'].set('data/hand_flex3_checks/wide_gyro')
        self.controls()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--person');p.add_argument('--session');p.add_argument('--split',choices=['train','validation','test'])
    p.add_argument('--output');p.add_argument('--count',type=int)
    a=p.parse_args()
    if a.count is not None and a.count<1:p.error('Count must be positive')
    root=tk.Tk();app=WideApp(root)
    for name,value in vars(a).items():
        if value is not None:app.vars[name].set(str(value))
    root.mainloop()


if __name__=='__main__':main()
