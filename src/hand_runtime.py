"""Independent hand windows and LOCAL action events; no gameplay transport."""
from collections import deque
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading
import time

import numpy as np
from src.hand_ai_flex3.core import CHANNELS, LABELS, resample

FORMAT = dict(activation_bits=32,activation_fraction=18,weight_bits=22,
              weight_fraction=21,accumulator_bits=64,axi_carrier_bits=32)
TOP = 'gesture_cnn_hand_u2_cache'
ONGOING = {'CHOP','WASH','POURING'}
PULSES = {'PICKUP','DROP','THROW'}


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def uint32(value):
    return type(value) is int and 0<=value<2**32


class ModelBundle:
    """Read and verify the complete export, including per-hand normalizers."""
    def __init__(self,folder,allow_synthetic=False):
        self.folder=Path(folder).resolve()
        self.manifest=json.loads((self.folder/'manifest.json').read_text(encoding='utf-8-sig'))
        manifest=self.manifest
        scope=manifest.get('scope')
        self.synthetic=scope=='synthetic_contract_test_only'
        if scope not in ('synthetic_contract_test_only','two_trained_hands_validation_export'):
            raise ValueError('Not an independent-hand export')
        if self.synthetic and not allow_synthetic:
            raise ValueError('Synthetic test bundle requires --allow-synthetic; it is not a trained model')
        if (manifest.get('top')!=TOP or manifest.get('format')!=FORMAT or
                manifest.get('cache_slots')!=2 or manifest.get('parallel_lanes')!=2):
            raise ValueError('Unsupported hand hardware/numeric contract')
        hashes=manifest.get('generated_sha256',{})
        required={'hand_contract.json','hand_cnn.cpp','hand_cnn.h','hand.cfg'}
        for hand in ('left','right'):
            required.update(hand+'/'+n for n in ('parameter_words.bin','normalization.npz','config.json'))
        if not required.issubset(hashes):raise ValueError('Export is incomplete')
        for name,expected in hashes.items():
            path=(self.folder/name).resolve()
            if not path.is_relative_to(self.folder) or not path.is_file() or digest(path)!=expected:
                raise ValueError('Export file missing or hash changed: '+name)
        contract=json.loads((self.folder/'hand_contract.json').read_text())
        if (contract.get('top')!=TOP or contract.get('features')!=CHANNELS or
                contract.get('classes')!=LABELS or contract.get('input_shape')!=[50,12] or
                contract.get('parameter_words')!=52967 or contract.get('output_words')!=7 or
                contract.get('format')!=FORMAT):
            raise ValueError('Feature/class/parameter contract mismatch')
        self.models={}
        rows=manifest.get('models',[])
        if len(rows)!=2:raise ValueError('Need two model records')
        for device,hand in ((1,'left'),(2,'right')):
            row=rows[device-1]
            if row.get('device_id')!=device or row.get('cache_slot')!=device-1 or not uint32(row.get('model_id')):
                raise ValueError('Model hand/cache identity mismatch')
            parameters=np.fromfile(self.folder/hand/'parameter_words.bin',dtype='<i4')
            if len(parameters)!=52967 or np.any(parameters[:52800]<-(1<<21)) or np.any(parameters[:52800]>=(1<<21)):
                raise ValueError('Bad parameter size or signed22 weight range')
            with np.load(self.folder/hand/'normalization.npz',allow_pickle=False) as z:
                mean,std=z['mean'].copy(),z['std'].copy()
            if (mean.shape!=(12,) or std.shape!=(12,) or not np.isfinite(mean).all() or
                    not np.isfinite(std).all() or np.any(std<=0)):
                raise ValueError('Invalid normalization')
            config=json.loads((self.folder/hand/'config.json').read_text())
            if config.get('feature_columns')!=CHANNELS or config.get('class_names')!=LABELS:
                raise ValueError('Model feature/class order differs')
            allowed_versions=('SYNTHETIC_TEST_ONLY',) if self.synthetic else ('independent_hand_flex3_v1','independent_hand_flex3_gyro_v5_v1')
            if config.get('version') not in allowed_versions:raise ValueError('Synthetic/trained configuration scope differs')
            if config.get('version')=='independent_hand_flex3_gyro_v5_v1':
                from src.hand_ai_flex3.core import CONFIG
                expected=dict(CONFIG,version='independent_hand_flex3_gyro_v5_v1',sensor_packet_version=5,
                              gyro_transport_scale=10,gyro_sensor_full_scale_dps=1000)
                if config!=expected:raise ValueError('Wide-gyro preprocessing contract differs')
            parameters.flags.writeable=False
            mean.flags.writeable=False;std.flags.writeable=False
            self.models[device]=dict(parameters=parameters,mean=mean,std=std,
                slot=device-1,model_id=row['model_id'],hash=digest(self.folder/hand/'parameter_words.bin'))

    def encode(self,device,window):
        model=self.models[device];window=np.asarray(window,dtype=np.float32)
        if window.shape!=(50,12) or not np.isfinite(window).all():raise ValueError('Expected finite (50,12) window')
        normalized=((window-model['mean'])/model['std']).astype(np.float32)
        words=np.floor(normalized.astype(np.float64)*2**18+.5)
        if not np.isfinite(words).all() or np.any(words<-(1<<31)) or np.any(words>=(1<<31)):
            raise ValueError('Normalized input exceeds A32/F18; no silent saturation')
        return words.astype(np.int32).reshape(-1)


@dataclass(frozen=True)
class Job:
    device: int
    boot: int
    seq: int
    uptime_ms: int
    generation: int
    received: float
    window: np.ndarray


class HandWindow:
    def __init__(self,device):
        self.device=device;self.generation=0;self.reset()

    def reset(self):
        self.rows=deque();self.previous=None;self.elapsed=0.;self.generation+=1

    def add(self,message,received):
        if message.get('device_id')!=self.device or not all(uint32(message.get(k)) for k in ('boot_id','seq','uptime_ms')):
            raise ValueError('Invalid hand/source identity')
        values=np.asarray(message.get('values'),dtype=np.float64)
        if values.shape!=(12,) or not np.isfinite(values).all() or not np.isfinite(received):
            raise ValueError('Expected twelve finite physical-unit features')
        boot,seq,uptime=(message[k] for k in ('boot_id','seq','uptime_ms'))
        if self.previous:
            pb,ps,pu,pr=self.previous
            if received<pr:raise ValueError('Host receive time moved backwards')
            if boot==pb:
                delta=(seq-ps)%2**32
                if delta==0 or delta>=2**31:return False
                dt=(uptime-pu)%2**32
                if delta!=1 or not 0<dt<=100 or received-pr>.5:self.reset()
                else:self.elapsed+=dt/1000.
            else:self.reset()
        self.previous=(boot,seq,uptime,float(received))
        self.rows.append((self.elapsed,values.copy()))
        while len(self.rows)>2 and self.rows[1][0]<=self.elapsed-2.1:self.rows.popleft()
        return True

    def job(self,now):
        if not self.previous or now-self.previous[3]>.5:return None
        start=self.elapsed-2.
        if not self.rows or self.rows[0][0]>start+1e-8:return None
        rows=list(self.rows)
        if sum(t>=start-1e-8 for t,v in rows)<85:return None
        window=resample([t for t,v in rows],[v for t,v in rows],np.linspace(start,self.elapsed,50))
        boot,seq,uptime,received=self.previous
        return Job(self.device,boot,seq,uptime,self.generation,received,window)


class ActionFilter:
    """Three stable predictions; pulses latch until confirmed idle/another label.

    Unknown confidence never becomes IDLE. Ongoing-state expiry is a local
    state_end event, which the current model bridge cannot transport.
    """
    def __init__(self,threshold=.70,confirmations=3,timeout=.6):
        if not 0<=threshold<=1 or confirmations<1 or timeout<=0:raise ValueError('Invalid confirmation settings')
        self.threshold=threshold;self.confirmations=confirmations;self.timeout=timeout
        self.candidate=None;self.count=0;self.accepted=None;self.active=None;self.last_good=None;self.active_good=None

    def reset(self,reason='reset'):
        events=[]
        if self.active:events.append(dict(type='state_end',gesture=self.active,reason=reason))
        self.candidate=None;self.count=0;self.accepted=None;self.active=None;self.last_good=None;self.active_good=None
        return events

    def tick(self,now):
        events=[]
        if self.active and now-self.active_good>self.timeout:
            events.append(dict(type='state_end',gesture=self.active,reason='prediction_timeout'))
            self.active=None;self.active_good=None
            if self.accepted in ONGOING:self.accepted=None
        if self.last_good is not None and now-self.last_good>self.timeout:
            self.candidate=None;self.count=0
            # Keep a one-shot latch through uncertainty. Never invent IDLE.
            if self.accepted in ONGOING:self.accepted=None
            self.last_good=None
        return events

    def update(self,gesture,confidence,now):
        if gesture not in LABELS or not np.isfinite(confidence) or not 0<=confidence<=1:
            raise ValueError('Invalid prediction')
        events=self.tick(now)
        if confidence<=self.threshold:
            self.candidate=None;self.count=0
            return events
        self.last_good=now
        if self.active==gesture:self.active_good=now
        self.count=self.count+1 if self.candidate==gesture else 1
        self.candidate=gesture
        if self.count<self.confirmations or gesture==self.accepted:return events
        if self.active:events.append(dict(type='state_end',gesture=self.active,reason='confirmed_transition'))
        self.active=None;self.active_good=None;self.accepted=gesture
        if gesture in PULSES:events.append(dict(type='action',gesture=gesture,confidence=confidence))
        elif gesture in ONGOING:
            self.active=gesture;self.active_good=now
            events.append(dict(type='state_start',gesture=gesture,confidence=confidence))
        else:events.append(dict(type='idle',gesture='IDLE',confidence=confidence))
        return events


class HandEngine:
    """Reception can run concurrently; one caller serializes backend inference."""
    def __init__(self,backend,threshold=.7,confirmations=3,interval=.2):
        if interval<=0:raise ValueError('Invalid prediction interval')
        self.backend=backend;self.interval=interval;self.lock=threading.Lock()
        self.windows={d:HandWindow(d) for d in (1,2)}
        self.filters={d:ActionFilter(threshold,confirmations) for d in (1,2)}
        self.due={d:0. for d in (1,2)};self.last={d:None for d in (1,2)};self.pending=[]

    def _events(self,device,events,now,job=None):
        return [dict(event,device_id=device,elapsed_s=now,
                     **(dict(boot_id=job.boot,seq=job.seq,uptime_ms=job.uptime_ms) if job else {})) for event in events]

    def add(self,message,received):
        device=message.get('device_id')
        if type(device) is not int or device not in (1,2):raise ValueError('Invalid device_id')
        with self.lock:
            window=self.windows[device];generation=window.generation
            added=window.add(message,received)
            if window.generation!=generation:
                self.pending+=self._events(device,self.filters[device].reset('source_reset'),received)
                self.last[device]=None;self.due[device]=received
            return added

    def reset(self,now):
        with self.lock:
            for device in (1,2):
                self.windows[device].reset()
                self.pending+=self._events(device,self.filters[device].reset('connection_reset'),now)
                self.last[device]=None;self.due[device]=now

    def process_due(self,now,clock=None):
        with self.lock:records,self.pending=self.pending,[]
        for device in (1,2):
            selected=clock() if clock else now
            with self.lock:
                records+=self._events(device,self.filters[device].tick(selected),selected)
                if selected+1e-9<self.due[device]:continue
                job=self.windows[device].job(selected)
                if job is None:continue
                identity=(job.boot,job.seq,job.generation)
                if identity==self.last[device]:continue
                self.last[device]=identity
                # Schedule from start, not inference completion. Do not build a work backlog.
                self.due[device]=selected+self.interval
            started=time.perf_counter();prob=np.asarray(self.backend.probabilities(device,job.window),dtype=float)
            inference_ms=(time.perf_counter()-started)*1000
            finished=clock() if clock else now
            if prob.shape!=(7,) or not np.isfinite(prob).all() or np.any(prob<0) or not np.isclose(prob.sum(),1.):
                raise ValueError('Backend must return seven finite probabilities summing to one')
            with self.lock:
                if job.generation!=self.windows[device].generation or finished-job.received>.5:
                    records.append(dict(type='stale_suppressed',device_id=device,elapsed_s=finished))
                    continue
                index=int(prob.argmax());gesture=LABELS[index];confidence=float(prob[index])
                record=dict(type='prediction',device_id=device,boot_id=job.boot,seq=job.seq,
                    uptime_ms=job.uptime_ms,elapsed_s=finished,gesture=gesture,confidence=confidence,
                    probabilities=prob.tolist(),inference_ms=inference_ms,
                    worker_sample_age_ms=max(0.,finished-job.received)*1000,
                    above_threshold=confidence>self.filters[device].threshold)
                timings=getattr(self.backend,'last_timings',{}).get(device)
                if timings:record['backend_timing_ms']=dict(timings)
                records.append(record)
                records+=self._events(device,self.filters[device].update(gesture,confidence,finished),finished,job)
        return records
