"""Create an isolated, deterministic 50-Hz-per-hand JSONL replay; no BLE."""
import argparse
import json
import math
from pathlib import Path
from src.hand_ai_flex3.core import CHANNELS


def generate(path,seconds=6.):
    if not 3<=seconds<=60:raise ValueError('Demo duration must be 3 to 60 seconds')
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8',newline='\n') as stream:
        stream.write(json.dumps(dict(type='hello',format=1,channels=CHANNELS))+'\n')
        for seq in range(int(seconds*50)+1):
            elapsed=seq/50.
            for device in (1,2):
                phase=elapsed*2+device
                values=[.1*math.sin(phase),.1*math.cos(phase),1.,
                        10*math.sin(phase),5*math.cos(phase),2.,
                        20*math.sin(phase),10*math.cos(phase),5.,
                        .5+.1*math.sin(phase),.5+.1*math.cos(phase),.5+.08*math.sin(phase)]
                stream.write(json.dumps(dict(type='sample',elapsed_s=elapsed,device_id=device,
                    boot_id=1000+device,seq=seq,uptime_ms=10000+seq*20,values=values))+'\n')
    print('Synthetic replay saved:',path.resolve())


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True)
    p.add_argument('--seconds',type=float,default=6.)
    a=p.parse_args();generate(a.out,a.seconds)


if __name__=='__main__':main()
