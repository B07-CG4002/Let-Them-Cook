"""Unix-socket client for Jie Yan's newline-JSON contract.
Pairing uses local receive time as an APPROXIMATION, not synchronized glove time.
Inference runs separately from reception; stale work is discarded.
"""
import argparse
from collections import deque
import json
import socket
import threading
import time
import numpy as np
from src.ai_common import resample
from src.board_backend import BoardPredictor
CHANNELS=['ax','ay','az','gx','gy','gz','yaw','pitch','roll','flex1','flex2']

class WindowBuffer:
    def __init__(self): self.reset()
    def reset(self):
        self.streams={1:deque(),2:deque()}; self.last={}; self.generation=getattr(self,'generation',0)+1
    def add(self,message,now):
        device=message['device_id']; boot=message['boot_id']; seq=message['seq']; uptime=message['uptime_ms']
        if device not in (1,2): raise ValueError('Unknown device_id')
        for v in (boot,seq,uptime):
            if type(v) is not int or not 0<=v<2**32: raise ValueError('Invalid uint32')
        values=np.asarray(message['values'],dtype=float)
        if values.shape!=(11,) or not np.isfinite(values).all(): raise ValueError('Invalid values')
        prev=self.last.get(device)
        if prev:
            if boot==prev[0] and seq<=prev[1]: return
            elapsed=(uptime-prev[2])%(2**32)
            if boot!=prev[0] or elapsed>100 or now-prev[3]>.100: self.reset()
        self.last[device]=(boot,seq,uptime,now)
        self.streams[device].append((now,values,(device,boot,seq)))
        for q in self.streams.values():
            while q and q[0][0]<now-2.5: q.popleft()
    def window(self,now):
        left,right=self.streams[1],self.streams[2]
        if not left or not right or now-min(left[-1][0],right[-1][0])>.1: return None
        end=min(left[-1][0],right[-1][0]); start=end-2
        if max(left[0][0],right[0][0])>start: return None
        target=np.linspace(start,end,50); result=[]
        for q in (left,right):
            times=np.array([row[0] for row in q]); vals=np.array([row[1] for row in q])
            if np.max(np.diff(times))>.1: return None
            nearest=np.min(np.abs(times[:,None]-target[None,:]),axis=0)
            if np.max(nearest)>.035: return None
            # Reuse the same angle-aware 22-channel resampler for each glove.
            paired=np.concatenate((vals,vals),axis=1)
            result.append(resample(times,paired,target)[:,:11])
        # Choose newest actual source sample at/before the window endpoint.
        used=[row for q in (left,right) for row in q if row[0]<=end]
        identity=max(used,key=lambda row:row[0])[2]
        return np.concatenate(result,axis=1),identity,self.generation,end

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--socket',default='/var/tmp/cg4002-week7-yanjie-20260907/model-runner.sock')
    p.add_argument('--weights',required=True); p.add_argument('--bit',help='Omit for explicitly CPU-only testing')
    p.add_argument('--control-address',type=lambda v:int(v,0),default=0x80010000)
    p.add_argument('--interval',type=float,default=.2); p.add_argument('--threshold',type=float,default=.70)
    a=p.parse_args()
    if a.interval<=0 or not 0<=a.threshold<=1: raise ValueError('Invalid interval/threshold')
    backend=BoardPredictor(a.weights,a.bit,a.control_address)
    lock=threading.Lock(); state=WindowBuffer(); connection={'socket':None}; stop=threading.Event()
    def inference():
        last_id=None
        while not stop.wait(a.interval):
            with lock:
                job=state.window(time.monotonic()) if connection['socket'] else None
            if job is None: continue
            values,identity,generation,end=job
            if identity==last_id: continue
            try: prediction=backend.predict_window(values)
            except Exception as error:
                print('Inference error; stopping worker:',error,flush=True); stop.set()
                with lock:
                    if connection['socket']:
                        try: connection['socket'].shutdown(socket.SHUT_RDWR)
                        except OSError: pass
                return
            if prediction['confidence']<a.threshold: continue  # suppress; do not relabel as IDLE
            result=dict(type='result',gesture=prediction['gesture'],confidence=prediction['confidence'],
                        device_id=identity[0],boot_id=identity[1],seq=identity[2])
            with lock:
                if state.generation!=generation or time.monotonic()-end>.5: continue
                client=connection['socket']
                if client:
                    try: client.sendall((json.dumps(result)+'\n').encode()); last_id=identity; print(result,flush=True)
                    except OSError: pass
    thread=threading.Thread(target=inference,daemon=True); thread.start()
    print('Low-confidence results suppressed. IDLE emitted when predicted above threshold.',flush=True)
    print('Receive-time alignment is approximate; validate under actual network delays.',flush=True)
    try:
        while not stop.is_set():
            try:
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                    client.settimeout(1); client.connect(a.socket)
                    with lock: state.reset(); connection['socket']=client
                    pending=b''; hello=False
                    while not stop.is_set():
                        try: chunk=client.recv(65536)
                        except socket.timeout: continue
                        if not chunk: break
                        pending+=chunk
                        if len(pending)>1048576: raise ValueError('Message exceeds limit')
                        while b'\n' in pending:
                            line,pending=pending.split(b'\n',1)
                            if not line.strip(): continue
                            message=json.loads(line)
                            if not hello:
                                if message.get('type')!='hello' or message.get('format')!=1 or message.get('channels')!=CHANNELS:
                                    raise ValueError('Unsupported hello contract')
                                hello=True; continue
                            if message.get('type')=='sample':
                                with lock: state.add(message,time.monotonic())
            except (OSError,ValueError,KeyError,TypeError) as error: print('Connection/sample error:',error,flush=True)
            finally:
                with lock: connection['socket']=None; state.reset()
            stop.wait(1)
    except KeyboardInterrupt: pass
    finally:
        stop.set(); thread.join(6); backend.close()
if __name__=='__main__': main()
