"""Bridge ultra96.server's start/on_sample API to the separate AI worker.
Standard library only: imported by normal-user /usr/bin/python3.
No inference, FPGA access or network writes occur inside on_sample().
"""
import atexit
from collections import OrderedDict, deque
import json
import logging
import math
import os
from pathlib import Path
import socket
import threading
import time

LOG = logging.getLogger(__name__)
CHANNELS = ['ax','ay','az','gx','gy','gz','yaw','pitch','roll','flex1','flex2']
DEFAULT_SOCKET = '/var/tmp/cg4002-week7-yanjie-20260907/model-runner.sock'


def uint32(value):
    return type(value) is int and 0 <= value < 2**32


class Adapter:
    """One inference-worker connection at a time; bounded freshness-first queue."""
    def __init__(self, path=DEFAULT_SOCKET, capacity=128, freshness=.1):
        self.path = Path(path)
        self.capacity = capacity
        self.freshness = freshness
        self.pending = deque()
        self.sent = OrderedDict()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.listener = None
        self.client = None
        self.thread = None
        self.emit = None
        self.socket_identity = None
        self.metrics = dict(enqueued=0,disconnected_dropped=0,queue_dropped=0,
                            expired=0,results=0,invalid_results=0,emit_refused=0)

    def start(self, emit):
        if self.thread is not None:
            raise RuntimeError('AI adapter already started')
        if not callable(emit):
            raise TypeError('emit must be callable')
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        # Never unlink another process's socket or an arbitrary existing file.
        if os.path.lexists(self.path):
            raise FileExistsError('AI socket already exists: '+str(self.path))
        listener = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            st=self.path.lstat(); self.socket_identity=(st.st_dev,st.st_ino)
            os.chmod(self.path,0o600)
            listener.listen(1); listener.settimeout(.5)
        except Exception:
            listener.close(); self._unlink_owned(); raise
        self.listener=listener; self.emit=emit
        self.thread=threading.Thread(target=self._serve,name='ai-comms-adapter',daemon=True)
        self.thread.start()
        atexit.register(self.close)
        LOG.info('AI adapter ready at %s; waiting for separate inference worker',self.path)

    def on_sample(self,device_id,boot_id,seq,uptime_ms,values):
        # Values have already been decoded into physical units by ModelPort.
        sample=dict(type='sample',device_id=int(device_id),boot_id=int(boot_id),
                    seq=int(seq),uptime_ms=int(uptime_ms),values=list(values))
        with self.lock:
            if self.client is None or self.stop.is_set():
                self.metrics['disconnected_dropped']+=1; return
            if len(self.pending)>=self.capacity:
                self.pending.popleft(); self.metrics['queue_dropped']+=1
            self.pending.append((time.monotonic(),sample))
            self.metrics['enqueued']+=1
        self.wake.set()

    def _next_sample(self):
        with self.lock:
            while self.pending:
                created,sample=self.pending.popleft()
                if time.monotonic()-created<=self.freshness:
                    return sample
                self.metrics['expired']+=1
        return None

    def _handle_result(self,message):
        if not isinstance(message,dict) or set(message)!={'type','gesture','confidence','device_id','boot_id','seq'}:
            self.metrics['invalid_results']+=1; return
        gesture=message['gesture']; confidence=message['confidence']; device=message['device_id']
        identity=(device,message['boot_id'],message['seq'])
        valid=(message['type']=='result' and type(device) is int and device in (1,2)
               and uint32(identity[1]) and uint32(identity[2])
               and isinstance(gesture,str) and 1<=len(gesture)<=32
               and all(c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in gesture)
               and type(confidence) in (int,float) and math.isfinite(confidence) and 0<=confidence<=1)
        if not valid:
            self.metrics['invalid_results']+=1; return
        with self.lock:
            if identity not in self.sent or self.sent[identity]:
                self.metrics['invalid_results']+=1; return
            self.sent[identity]=True
        # Server's emit is explicitly thread-safe; it validates again and schedules
        # accepted result delivery on its own event loop.
        accepted=self.emit(gesture,float(confidence),*identity)
        self.metrics['results']+=1
        if accepted is False: self.metrics['emit_refused']+=1

    def _read_results(self,client,done):
        pending=b''
        try:
            while not done.is_set() and not self.stop.is_set():
                try: chunk=client.recv(65536)
                except socket.timeout: continue
                if not chunk: break
                pending+=chunk
                if len(pending)>1048576: raise ValueError('AI result exceeds size limit')
                while b'\n' in pending:
                    line,pending=pending.split(b'\n',1)
                    if line.strip(): self._handle_result(json.loads(line))
        except (OSError,ValueError,TypeError):
            LOG.warning('AI worker connection/result error',exc_info=True)
        finally:
            done.set(); self.wake.set()

    def _serve(self):
        while not self.stop.is_set():
            try: client,_=self.listener.accept()
            except socket.timeout: continue
            except OSError: break
            done=threading.Event(); reader=None
            try:
                client.settimeout(.5)
                client.sendall((json.dumps(dict(type='hello',format=1,channels=CHANNELS))+'\n').encode())
                with self.lock:
                    self.pending.clear(); self.sent.clear(); self.client=client
                LOG.info('AI worker connected')
                reader=threading.Thread(target=self._read_results,args=(client,done),daemon=True)
                reader.start()
                while not done.is_set() and not self.stop.is_set():
                    self.wake.clear()
                    sample=self._next_sample()
                    if sample is None:
                        self.wake.wait(.02); continue
                    identity=(sample['device_id'],sample['boot_id'],sample['seq'])
                    with self.lock:
                        self.sent[identity]=False
                        while len(self.sent)>4096: self.sent.popitem(last=False)
                    client.sendall((json.dumps(sample,allow_nan=False)+'\n').encode())
            except (OSError,ValueError):
                if not self.stop.is_set(): LOG.warning('AI worker disconnected during sample forwarding')
            finally:
                done.set()
                with self.lock:
                    self.client=None; self.pending.clear(); self.sent.clear()
                try: client.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                client.close()
                if reader: reader.join(1)

    def _unlink_owned(self):
        try:
            st=self.path.lstat()
            if (st.st_dev,st.st_ino)==self.socket_identity: self.path.unlink()
        except FileNotFoundError: pass

    def close(self):
        self.stop.set(); self.wake.set()
        if self.listener: self.listener.close()
        with self.lock: client=self.client
        if client:
            try: client.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        if self.thread and self.thread is not threading.current_thread(): self.thread.join(2)
        self._unlink_owned()


_adapter=None

def start(emit):
    global _adapter
    if _adapter is not None: raise RuntimeError('Adapter already started')
    adapter=Adapter(os.environ.get('CG4002_AI_SOCKET',DEFAULT_SOCKET))
    adapter.start(emit); _adapter=adapter

def on_sample(device_id,boot_id,seq,uptime_ms,values):
    if _adapter is None: raise RuntimeError('start(emit) must be called first')
    _adapter.on_sample(device_id,boot_id,seq,uptime_ms,values)

def close():
    if _adapter: _adapter.close()
