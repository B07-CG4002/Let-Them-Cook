"""Laptop CPU inference over BLE. Prints diagnostics; sends no game commands."""
import argparse
import asyncio
from collections import deque
import json
import time
import numpy as np
from src.hand_ai.core import LABELS, resample
from src.hand_ai.pipeline import Predictor
from src.hand_ai.ble import connection, ensure_healthy

class HandWindow:
    def __init__(self): self.clear()
    def clear(self):
        self.rows=deque(); self.previous=None; self.elapsed=0.
    def add(self, packet, values, received):
        if self.previous is not None:
            p=self.previous; dt=(packet.uptime_ms-p.uptime_ms)%2**32
            if packet.boot_id!=p.boot_id or packet.seq!=(p.seq+1)%2**32 or not 0<dt<=100:
                self.clear()
            else: self.elapsed+=dt/1000.
        self.previous=packet
        self.rows.append((self.elapsed,values,packet,received))
        while len(self.rows)>2 and self.rows[1][0]<=self.elapsed-2.1: self.rows.popleft()
    def window(self):
        if not self.rows or self.rows[0][0]>self.elapsed-2.+1e-8: return None
        return resample([r[0] for r in self.rows],[r[1] for r in self.rows],
                        np.linspace(self.elapsed-2.,self.elapsed,50))

async def run(a):
    predictor=Predictor(a.run)
    if predictor.device_id!=a.device:
        raise ValueError('Model trained for another hand; cross-hand use needs separate validation')
    state=HandWindow();last=0.
    async with connection(a.device,a.address) as (_,queue,health):
        print(f'Collecting 2 seconds. Showing predictions only when confidence > {a.threshold:.2f}.',flush=True)
        while True:
            ensure_healthy(health)
            try: packet,values,received=await asyncio.wait_for(queue.get(),3)
            except asyncio.TimeoutError:
                state.clear();raise ConnectionError('No BLE samples for 3 seconds')
            if time.monotonic()-received>.5:
                state.clear();continue
            state.add(packet,values,received)
            if time.monotonic()-last<a.interval or not queue.empty():continue
            window=state.window()
            if window is None:continue
            started=time.monotonic()
            prob=await asyncio.to_thread(predictor.probabilities,window)
            done=time.monotonic();last=done
            if done-received>.5:
                print('Suppressed stale inference',flush=True);continue
            index=int(prob[0].argmax());confidence=float(prob[0,index])
            if confidence<=a.threshold:continue
            print(json.dumps(dict(device_id=a.device,boot_id=packet.boot_id,seq=packet.seq,
                  gesture=LABELS[index],confidence=round(confidence,4),
                  status='VALID',
                  inference_ms=round((done-started)*1000,2),sample_age_ms=round((done-received)*1000,2))),flush=True)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',required=True);p.add_argument('--device',type=int,choices=(1,2),default=2)
    p.add_argument('--address');p.add_argument('--threshold',type=float,default=.7,help='Provisional; choose on validation')
    p.add_argument('--interval',type=float,default=.2);a=p.parse_args()
    if not 0<=a.threshold<=1 or a.interval<=0:p.error('Invalid threshold/interval')
    try:asyncio.run(run(a))
    except KeyboardInterrupt:print('Stopped.')

if __name__=='__main__':main()
