"""Integer NumPy reference and PYNQ driver for the NEW independent-hand IP."""
from pathlib import Path
import threading
import time

import numpy as np
from src.hand_runtime import TOP,digest


def integer_logits(inputs,parameters):
    """Exact signed64 sums and one round/saturate per layer; A32/F18 W22/F21."""
    x=np.asarray(inputs);p=np.asarray(parameters)
    if x.shape!=(600,) or x.dtype.kind not in 'iu' or p.shape!=(52967,) or p.dtype.kind not in 'iu':
        raise ValueError('Bad integer word dimensions/type')
    if (np.any(x<-(1<<31)) or np.any(x>=(1<<31)) or
            np.any(p[:52800]<-(1<<21)) or np.any(p[:52800]>=(1<<21)) or
            np.any(p[52800:]<-(1<<31)) or np.any(p[52800:]>=(1<<31))):
        raise ValueError('Integer words exceed hardware representation')
    x=x.astype(np.int64).reshape(50,12).T;p=p.astype(np.int64)
    def finish(value):return np.clip((value+(1<<20))>>21,-(1<<31),(1<<31)-1)
    def conv(v,weights,bias):
        windows=np.lib.stride_tricks.sliding_window_view(v,3,axis=1)
        columns=windows.transpose(1,0,2).reshape(windows.shape[1],-1)
        result=finish(columns@weights.reshape(len(weights),-1).T+bias*(1<<21)).T
        result=np.maximum(result,0)
        return result.reshape(len(weights),result.shape[1]//2,2).max(axis=2)
    x=conv(x,p[:1152].reshape(32,12,3),p[52800:52832])
    x=conv(x,p[1152:7296].reshape(64,32,3),p[52832:52896]).reshape(-1)
    x=np.maximum(finish(p[7296:52352].reshape(64,704)@x+p[52896:52960]*(1<<21)),0)
    return finish(p[52352:52800].reshape(7,64)@x+p[52960:]*(1<<21)).astype(np.int32)


def softmax(words):
    logits=np.asarray(words,dtype=np.float64)/2**18
    if logits.shape!=(7,) or not np.isfinite(logits).all():raise ValueError('Bad FPGA logits')
    values=np.exp(logits-logits.max());return values/values.sum()


class NumpyHandBackend:
    """Offline numerical reference; its timing is not FPGA timing."""
    def __init__(self,bundle):self.bundle=bundle
    def probabilities(self,device,window):
        return softmax(integer_logits(self.bundle.encode(device,window),self.bundle.models[device]['parameters']))
    def close(self):pass


def pointer_offsets(registers,name):
    if name in registers and int(registers[name]['size'])==64:
        offset=int(registers[name]['address_offset']);return offset,offset+4
    if all(n in registers and int(registers[n]['size'])==32 for n in (name+'_1',name+'_2')):
        return tuple(int(registers[n]['address_offset']) for n in (name+'_1',name+'_2'))
    raise ValueError('Cannot locate 64-bit HWH pointer '+name)


class PynqHandBackend:
    """New-bitstream driver. Instantiation downloads an overlay; never implicit.

    Register offsets come from HWH, not the old paired driver. A lock serializes
    the shared engine. Parameters are flushed once; each hand is forced into its
    slot on first use, then AUTO permits warm hits. Timeout poisons this driver.
    """
    def __init__(self,bundle,bit,ip_name=TOP+'_0',timeout=5.,overlay_factory=None,allocator=None):
        self.bundle=bundle;self.buffers=[];self.lock=threading.Lock();self.poisoned=False;self.closed=False
        self.last_timings={}
        self.timeout=float(timeout);self.loaded={1:False,2:False};self.last_status={}
        if self.timeout<=0:raise ValueError('Invalid FPGA timeout')
        bit=Path(bit)
        if not bit.is_file() or not bit.with_suffix('.hwh').is_file():
            raise ValueError('Need the NEW matching .bit and same-name .hwh files')
        self.artifacts=dict(bit=str(bit.resolve()),bit_sha256=digest(bit),hwh_sha256=digest(bit.with_suffix('.hwh')))
        if overlay_factory is None or allocator is None:
            from pynq import Overlay,allocate
            overlay_factory=overlay_factory or Overlay;allocator=allocator or allocate
        self.overlay=overlay_factory(str(bit))
        if ip_name not in self.overlay.ip_dict:raise ValueError('New hand IP missing; paired overlay is incompatible')
        self.ip=getattr(self.overlay,ip_name)
        registers=self.overlay.ip_dict[ip_name]['registers']
        self.pointers={n:pointer_offsets(registers,n) for n in ('input_words','parameter_words','logit_words')}
        def scalar(*names):
            for name in names:
                if name in registers and int(registers[name]['size'])==32:return int(registers[name]['address_offset'])
            raise ValueError('Required 32-bit register absent: '+str(names)+'; inspect the generated HWH')
        self.control=scalar('AP_CTRL','CTRL')
        self.return_offset=scalar('ap_return','return')
        self.controls={n:scalar(n) for n in ('cache_slot','model_id','cache_command')}
        try:
            self.x=allocator(shape=(600,),dtype=np.int32);self.buffers.append(self.x)
            self.out=allocator(shape=(7,),dtype=np.int32);self.buffers.append(self.out)
            self.params={}
            for device in (1,2):
                buf=allocator(shape=(52967,),dtype=np.int32);self.buffers.append(buf)
                buf[:]=bundle.models[device]['parameters'];buf.flush();self.params[device]=buf
            self._pointer('input_words',self.x);self._pointer('logit_words',self.out)
        except Exception:
            for buf in self.buffers:buf.freebuffer()
            self.buffers=[];raise

    def _pointer(self,name,buffer):
        low,high=self.pointers[name];address=int(buffer.device_address)
        self.ip.write(low,address&0xffffffff);self.ip.write(high,(address>>32)&0xffffffff)

    def run_words(self,device,words):
        model=self.bundle.models[device]
        words=np.asarray(words)
        if words.shape!=(600,) or words.dtype.kind not in 'iu' or np.any(words<-(1<<31)) or np.any(words>=(1<<31)):
            raise ValueError('Expected 600 signed32 input words')
        with self.lock:
            if self.closed:raise RuntimeError('FPGA driver is closed')
            if self.poisoned:raise RuntimeError('FPGA driver is poisoned after an error; stop and inspect/reset the hardware')
            setup_started=time.perf_counter()
            self.x[:]=words;self.x.flush();self._pointer('parameter_words',self.params[device])
            force=not self.loaded[device]
            for name,value in dict(cache_slot=model['slot'],model_id=model['model_id'],cache_command=1 if force else 0).items():
                self.ip.write(self.controls[name],value)
            started=time.perf_counter();self.ip.write(self.control,1);deadline=time.monotonic()+self.timeout
            while not self.ip.read(self.control)&2:
                if time.monotonic()>deadline:
                    self.poisoned=True;raise TimeoutError('Hand FPGA timeout; no retry or automatic overlay reset')
                time.sleep(.0001)
            done=time.perf_counter()
            status=int(self.ip.read(self.return_offset));self.last_status[device]=status
            if status not in (0,1) or (force and status!=1):
                self.poisoned=True;raise RuntimeError('Unexpected FPGA cache return status: '+str(status))
            self.loaded[device]=True
            self.out.invalidate()
            result=np.array(self.out,dtype=np.int32,copy=True)
            self.last_timings[device]=dict(input_setup_ms=(started-setup_started)*1000,
                start_poll_ms=(done-started)*1000,output_read_ms=(time.perf_counter()-done)*1000)
            return result

    def probabilities(self,device,window):
        started=time.perf_counter();words=self.bundle.encode(device,window)
        encoded=time.perf_counter();result=self.run_words(device,words)
        logits_ready=time.perf_counter();prob=softmax(result)
        self.last_timings[device].update(input_encode_ms=(encoded-started)*1000,
                                        softmax_ms=(time.perf_counter()-logits_ready)*1000)
        return prob

    def close(self):
        with self.lock:
            if self.buffers and self.poisoned and not self.ip.read(self.control)&4:
                raise RuntimeError('IP is still busy; buffers retained until hardware is stopped/reset')
            for buffer in self.buffers:buffer.freebuffer()
            self.buffers=[];self.closed=True
