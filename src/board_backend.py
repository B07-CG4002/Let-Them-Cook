"""NumPy CPU backend, optionally accelerating ONLY Conv1 using the existing IP.
No PyTorch import on the board. Existing bitstream requires unchanged Conv1 types.
"""
import json
import time
import numpy as np
from src.ai_common import CLASS_NAMES,CONFIG

def conv(x,w,b):
    windows=np.lib.stride_tricks.sliding_window_view(x,3,axis=1)
    return np.einsum('ctk,ock->ot',windows,w)+b[:,None]

def pool(x):
    x=np.maximum(x,0); n=x.shape[1]//2
    return x[:,:2*n].reshape(x.shape[0],n,2).max(axis=2)

def fixed_words(v,width,frac):
    q=np.floor(np.asarray(v,dtype=np.float64)*(1<<frac)+.5)
    q=np.clip(q,-(1<<(width-1)),(1<<(width-1))-1)
    return q.astype(np.int64).view(np.uint64).ravel()

class BoardPredictor:
    def __init__(self,weights_path,bit=None,control_address=0x80010000):
        with np.load(weights_path,allow_pickle=False) as z:
            if json.loads(str(z['config_json']))!=CONFIG: raise ValueError('Wrong preprocessing configuration')
            self.p={k:np.array(z[k],dtype=np.float32) for k in ('conv1_w','conv1_b','conv2_w','conv2_b','fc1_w','fc1_b','fc2_w','fc2_b')}
        shapes={'conv1_w':(32,22,3),'conv1_b':(32,),'conv2_w':(64,32,3),'conv2_b':(64,),
                'fc1_w':(64,704),'fc1_b':(64,),'fc2_w':(8,64),'fc2_b':(8,)}
        for k,s in shapes.items():
            if self.p[k].shape!=s or not np.isfinite(self.p[k]).all(): raise ValueError(f'Invalid {k}')
        self.buffers=[]; self.bit=bit
        if bit:
            if np.any(self.p['conv1_w'] < -1) or np.any(self.p['conv1_w'] > 1-2**-21):
                raise ValueError('Conv1 weights exceed fixed-point range')
            if np.any(self.p['conv1_b'] < -4096) or np.any(self.p['conv1_b'] > 4096-2**-27):
                raise ValueError('Conv1 bias exceeds fixed-point range')
            from pynq import Overlay,MMIO,allocate
            self.ol=Overlay(bit); self.ip=self.ol.conv1d_layer1_0; self.ctrl=MMIO(control_address,0x100)
            try:
                for n,dtype in [(1100,np.uint32),(2112,np.uint32),(32,np.uint64),(1536,np.uint64)]:
                    self.buffers.append(allocate(shape=(n,),dtype=dtype))
                self.x,self.w,self.b,self.out=self.buffers
                # Explicit masks avoid unsigned narrowing warnings across NumPy versions.
                self.w[:]=(fixed_words(self.p['conv1_w'],22,21)&0xffffffff).astype(np.uint32)
                self.b[:]=fixed_words(self.p['conv1_b'],40,27); self.w.flush(); self.b.flush()
                for name,buf in zip(('input_r','weights','bias','output_r'),self.buffers):
                    addr=int(buf.device_address)
                    setattr(self.ip.register_map,name+'_1',addr&0xffffffff)
                    setattr(self.ip.register_map,name+'_2',addr>>32)
            except Exception:
                self.close(); raise
            print('Loaded bitstream:',bit,flush=True)
            print('Inference: FPGA Conv1; remaining layers on Ultra96 CPU.',flush=True)
        else: print('Inference: CPU NumPy only (no FPGA acceleration).',flush=True)
    def predict_window(self,window):
        x=np.asarray(window,dtype=np.float32)
        if x.shape!=(50,22) or not np.isfinite(x).all(): raise ValueError('Expected finite (50,22)')
        p=self.p
        if self.bit:
            if np.any(x < -512) or np.any(x > 512-2**-18): raise ValueError('Input exceeds FPGA fixed-point range')
            self.x[:]=(fixed_words(x,28,18)&0xffffffff).astype(np.uint32); self.x.flush()
            self.out[:]=0; self.out.flush(); self.ctrl.write(0,1); deadline=time.monotonic()+5
            while not self.ctrl.read(0)&2:
                if time.monotonic()>deadline: raise TimeoutError('Conv1 timeout; restart worker and reload overlay')
                time.sleep(.0001)
            self.out.invalidate(); raw=np.asarray(self.out,dtype=np.uint64)&((1<<40)-1)
            signed=raw.astype(np.int64); signed=(signed^(1<<39))-(1<<39)
            first=(signed.astype(np.float64)/(1<<27)).reshape(32,48).astype(np.float32)
        else: first=conv(x.T,p['conv1_w'],p['conv1_b'])
        second=pool(conv(pool(first),p['conv2_w'],p['conv2_b']))
        hidden=np.maximum(p['fc1_w']@second.ravel()+p['fc1_b'],0)
        logits=p['fc2_w']@hidden+p['fc2_b']; prob=np.exp(logits-logits.max()); prob/=prob.sum(); i=int(prob.argmax())
        return dict(gesture=CLASS_NAMES[i],confidence=float(prob[i]),probabilities=prob.tolist())
    def close(self):
        for buf in self.buffers: buf.freebuffer()
        self.buffers=[]
