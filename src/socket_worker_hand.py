"""Observe/replay a twelve-channel bridge and write LOCAL diagnostic events.

This first worker does NOT send action events into the existing game bridge.
That bridge strips state/event metadata. Agree the state-end/expiry contract
before enabling gameplay transport in a later integration change.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import socket
import threading
import time

from src.hand_runtime import CHANNELS,ModelBundle,HandEngine,digest
from src.hand_backend import NumpyHandBackend,PynqHandBackend


def validate_hello(message):
    if not isinstance(message,dict) or message.get('type')!='hello' or message.get('format')!=1 or message.get('channels')!=CHANNELS:
        raise ValueError('Need bridge format 1 with exactly twelve ordered channels. Existing eleven-channel/v3 bridge is incompatible.')


class Recorder:
    def __init__(self,path,backend,synthetic):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.stream=path.open('x',encoding='utf-8',newline='\n')
        self.counts=Counter();self.devices={1:Counter(),2:Counter()}
        self.write(dict(type='scope',backend=backend,synthetic=synthetic,
            gameplay_transport=False,final_gesture_accuracy_validated=False,
            timing_scope='worker processing only; source/host clocks are not synchronized'))
    def write(self,record):
        self.stream.write(json.dumps(record,allow_nan=False)+'\n');self.stream.flush()
        self.counts[record['type']]+=1
        if record.get('device_id') in self.devices:self.devices[record['device_id']][record['type']]+=1
        if record['type'] in ('action','state_start','state_end','idle'):print(json.dumps(record),flush=True)
    def close(self):
        summary=dict(type='summary',status='failed' if self.counts['error'] else 'finished',
                     counts=dict(self.counts),hands={str(k):dict(v) for k,v in self.devices.items()})
        self.stream.write(json.dumps(summary)+'\n');self.stream.close();print(json.dumps(summary),flush=True)


def replay(path,engine,record,initialize=None):
    hello=False;previous=-float('inf');now=0.
    with Path(path).open(encoding='utf-8-sig') as stream:
        for line in stream:
            if not line.strip():continue
            message=json.loads(line)
            if not hello:
                validate_hello(message);hello=True
                if initialize:initialize()
                continue
            if not isinstance(message,dict) or message.get('type')!='sample':raise ValueError('Replay requires sample lines after hello')
            now=message.get('elapsed_s')
            if type(now) not in (float,int) or not 0<=now<float('inf') or now<previous:raise ValueError('Replay times must be finite and ordered')
            previous=now;engine.add(message,float(now))
            for event in engine.process_due(float(now)):record(event)
    if not hello:raise ValueError('Replay is empty')
    for event in engine.process_due(now+1.):record(event)


def connect_bridge(path):
    """Production transport is Unix-domain IPC on Ultra96/Linux, not replay."""
    family=getattr(socket,'AF_UNIX',None)
    if family is None:
        raise RuntimeError('This Python build has no Unix sockets. Use --replay on Windows; run --socket on Ultra96/Linux.')
    client=socket.socket(family,socket.SOCK_STREAM)
    try:
        client.settimeout(1);client.connect(path)
    except BaseException:
        client.close();raise
    return client


def observe_socket(path,engine,record,initialize=None,connection_factory=None):
    """Receiver continues while the single inference thread runs the backend."""
    stop=threading.Event();ready=threading.Event();errors=[]
    with (connection_factory or connect_bridge)(path) as client:
        def receive():
            pending=b'';hello=False
            try:
                while not stop.is_set():
                    try:data=client.recv(65536)
                    except socket.timeout:continue
                    if not data:break
                    pending+=data
                    while b'\n' in pending:
                        line,pending=pending.split(b'\n',1)
                        if len(line)>4096:raise ValueError('Bridge line too large')
                        if not line.strip():continue
                        message=json.loads(line)
                        if not hello:validate_hello(message);hello=True;ready.set();continue
                        if not isinstance(message,dict) or message.get('type')!='sample':raise ValueError('Unexpected bridge message')
                        engine.add(message,time.monotonic())
                    if len(pending)>4096:raise ValueError('Bridge line too large')
                if pending.strip():raise ValueError('Truncated bridge line')
                if not hello:raise ValueError('Bridge closed before hello')
            except Exception as error:errors.append(error)
            finally:stop.set();engine.reset(time.monotonic())
        receiver=threading.Thread(target=receive,name='hand-receive',daemon=True);receiver.start()
        initialized=False
        try:
            while not stop.wait(.005):
                if not ready.is_set():continue
                if not initialized:
                    if initialize:initialize()
                    initialized=True
                for event in engine.process_due(time.monotonic(),clock=time.monotonic):record(event)
        finally:
            stop.set()
            try:client.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            receiver.join(2)
            for event in engine.process_due(time.monotonic(),clock=time.monotonic):record(event)
        if errors:raise errors[0]


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle',required=True);p.add_argument('--allow-synthetic',action='store_true')
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--replay');source.add_argument('--socket')
    p.add_argument('--backend',choices=('numpy','fpga'),default='numpy')
    p.add_argument('--bit');p.add_argument('--ip-name',default='gesture_cnn_hand_u2_cache_0')
    p.add_argument('--threshold',type=float,default=.7);p.add_argument('--confirmations',type=int,default=3)
    p.add_argument('--interval',type=float,default=.2);p.add_argument('--out',required=True)
    a=p.parse_args(argv)
    if a.socket and not hasattr(socket,'AF_UNIX'):
        p.error('This Python build has no Unix sockets. Use --replay on Windows; run --socket on Ultra96/Linux.')
    if a.backend=='fpga' and not a.bit:p.error('FPGA mode requires the NEW matching --bit and .hwh')
    if a.backend=='numpy' and a.bit:p.error('--bit is only meaningful with --backend fpga')
    bundle=ModelBundle(a.bundle,a.allow_synthetic)
    # Validate settings before allocating buffers or downloading an overlay.
    engine=HandEngine(None,a.threshold,a.confirmations,a.interval)
    recorder=Recorder(a.out,a.backend,bundle.synthetic);backend=None
    recorder.write(dict(type='bundle_verified',manifest_sha256=digest(bundle.folder/'manifest.json'),
                        model_parameter_sha256={str(d):bundle.models[d]['hash'] for d in (1,2)}))
    def initialize():
        nonlocal backend
        backend=(NumpyHandBackend(bundle) if a.backend=='numpy' else PynqHandBackend(bundle,a.bit,a.ip_name))
        engine.backend=backend
        if a.backend=='fpga':recorder.write(dict(type='hardware_artifacts',**backend.artifacts))
    try:
        if a.replay:replay(a.replay,engine,recorder.write,initialize)
        else:observe_socket(a.socket,engine,recorder.write,initialize)
    except BaseException as error:
        recorder.write(dict(type='error',error_type=type(error).__name__,message=str(error)))
        raise
    finally:
        try:
            if backend:backend.close()
        finally:recorder.close()


if __name__=='__main__':main()
