"""PYNQ driver for gesture_cnn_full: ALL trained CNN layers in FPGA.
CPU only packs inputs, starts IP, waits, decodes eight logits and formats softmax.
Requires NEW full-network bitstream/HWH, never the old Conv1 bitstream.
"""
import json
import time
from pathlib import Path
import numpy as np
from src.export_full_cnn import encode,NAMES,SHAPES,PARAMS
CLASSES=['PICKUP','DROP','CHOP','WASH','THROW','FIREEXT','POURING','IDLE']

def pointer_registers(registers,name):
    """Handle the two common HWH representations of a 64-bit AXI pointer."""
    if name in registers and int(registers[name]['size'])==64:
        low=int(registers[name]['address_offset']);return low,low+4
    if name+'_1' in registers and name+'_2' in registers:
        return int(registers[name+'_1']['address_offset']),int(registers[name+'_2']['address_offset'])
    raise ValueError(f'Cannot locate 64-bit pointer {name}; registers={list(registers)}')

class FullCNNPredictor:
    def __init__(self,weights_path,bit,control_address=None,ip_name='gesture_cnn_full_0'):
        from pynq import Overlay,allocate
        if not bit: raise ValueError('Full-network bitstream is required')
        self.buffers=[]
        with np.load(weights_path,allow_pickle=False) as z:
            config=json.loads(str(z['config_json']));self.class_names=config['class_names']
            if self.class_names!=CLASSES:raise ValueError('Wrong class mapping')
            arrays=[]
            for name,shape in zip(NAMES,SHAPES):
                values=np.asarray(z[name],dtype=np.float32)
                if values.shape!=shape:raise ValueError(f'Wrong parameter {name}')
                arrays.append(encode(values,21 if name.endswith('_w') else 18,22 if name.endswith('_w') else 32).ravel())
        params=np.concatenate(arrays).astype(np.int32)
        if len(params)!=PARAMS:raise ValueError('Wrong parameter count')
        self.ol=Overlay(bit)
        if ip_name not in self.ol.ip_dict:
            raise ValueError(f'Full CNN IP {ip_name} missing; IPs={list(self.ol.ip_dict)}. Do not use the Conv1-only bitstream.')
        self.ip=getattr(self.ol,ip_name);description=self.ol.ip_dict[ip_name]
        self.base=int(description['phys_addr'])
        if control_address is not None and int(control_address)!=self.base:
            raise ValueError(f'Control address mismatch; HWH gives {hex(self.base)}')
        registers=description['registers']
        try:
            for count in (1100,PARAMS,8):self.buffers.append(allocate(shape=(count,),dtype=np.int32))
            self.x,self.params,self.out=self.buffers
            self.params[:]=params;self.params.flush()
            for name,buf in zip(('input_words','parameter_words','logit_words'),self.buffers):
                lo,hi=pointer_registers(registers,name);addr=int(buf.device_address)
                self.ip.write(lo,addr&0xffffffff);self.ip.write(hi,(addr>>32)&0xffffffff)
        except Exception:self.close();raise
        print('Loaded FULL CNN bitstream:',bit,flush=True)
        print('IP:',ip_name,'control:',hex(self.base),flush=True)
        print('All CNN layers execute in FPGA; CPU handles packing, polling and softmax.',flush=True)
    def run_words(self,input_words):
        values=np.asarray(input_words)
        if values.size!=1100 or values.dtype.kind not in 'iu':raise ValueError('Expected 1100 integer words')
        if np.any(values<-(1<<31)) or np.any(values>(1<<31)-1):raise ValueError('Word range error')
        self.x[:]=values.reshape(-1).astype(np.int32);self.x.flush()
        self.out[:]=0;self.out.flush()
        started=time.perf_counter();self.ip.write(0,1);deadline=time.monotonic()+10
        while not self.ip.read(0)&2:
            if time.monotonic()>deadline:raise TimeoutError('Full CNN timeout; inspect clocks/reset/AXI connections')
            time.sleep(.0001)
        self.out.invalidate();result=np.array(self.out,dtype=np.int32,copy=True)
        self.last_call_seconds=time.perf_counter()-started
        return result
    def predict_window(self,window):
        values=np.asarray(window,dtype=np.float32)
        if values.shape!=(50,22):raise ValueError('Expected (50,22)')
        words=self.run_words(encode(values,18,32))
        logits=words.astype(np.float64)/2**18
        probabilities=np.exp(logits-logits.max());probabilities/=probabilities.sum();i=int(probabilities.argmax())
        return dict(gesture=self.class_names[i],confidence=float(probabilities[i]),probabilities=probabilities.tolist())
    def close(self):
        for buf in self.buffers:buf.freebuffer()
        self.buffers=[]
