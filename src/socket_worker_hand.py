"""Observe/replay a twelve-channel bridge; in socket mode deliver COMPAT results and hand events to it.

Socket mode (--socket: the model bridge of ultra96.server --sensor-version 4
--model ultra96.model_bridge) sends results back in the bridge's runner format
(ultra96/model_bridge.py), one compact JSON line per result:
  {"type":"result","gesture":G,"confidence":C,"device_id":D,"boot_id":B,"seq":S}
COMPAT delivery: EventEncoder turns the engine records into hand events; each
pulse, state_start, state_refresh and idle event is one result line, D the hand
and B/S the event's source sample, which the service publishes as one v3
GESTURE_RESULT. state_end and reset events have no v3 form: they are recorded
here and never sent as results. A second result for one (hand, boot_id, seq) is
dropped and counted, as the service would refuse it. Each hand_event record says
whether its result line was sent (delivery: forwarded) or not (unsent: that
connection had failed or ended).
HAND EVENTS, only when the bridge hello offers them ("hand_events":1): once per
connection, before any other line, the hello H of that connection's encoder; then
one line per encoder event of every kind, state_end and reset too, the event dict
E unchanged, sent before that event's result line:
  {"type":"hand_hello","hello":H}
  {"type":"hand_event","event":E}
hand_delivery on each hand_event record says sent, unsent or not_offered (a
bridge without the offer gets the result lines alone, as before). A failed send of
either line ends that connection for both.
--observe-only keeps the earlier behaviour (record only, send nothing); --replay
never sends.

One HandEngine and one EventEncoder serve both hands. A glove reconnect or
reboot reaches this worker only through its samples: HandWindow resets that hand
alone on a new boot_id, a seq jump, an uptime step outside 0..100 ms or a receive
gap over 0.5 s, and the engine ends its state (source_reset); the other hand is
not touched. An encoder error (ProtocolError) is recorded and resets that hand's
encoder state only.

Socket mode runs until SIGTERM or Ctrl+C, which end it with its summary. It waits
while the service's socket is absent or refused, and connects again 1 s after a
bridge connection ends (the service stopped or restarted, or another runner took
the socket). Each end resets both hands; each connection gets a new encoder (a
new worker_epoch and hand_hello, event_seq from 1 per hand) with no state or sent
memory from the last, while the backend and its overlay stay loaded. An
incompatible hello, a bad bridge line or a backend error ends the worker.

--out names a new JSONL file, or an existing folder for a new file named from the
UTC start time and the PID. Times are this worker's monotonic clock; source and
host clocks are not synchronized.
"""
import argparse
from collections import Counter,OrderedDict
from datetime import datetime,timezone
import json
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import threading
import time

from common.hand_contract import ProtocolError,make_hello
from src.hand_runtime import CHANNELS,ModelBundle,HandEngine,digest
from src.hand_backend import NumpyHandBackend,PynqHandBackend
from src.hand_events import EventEncoder

DELIVERY='compat_v3_gesture_result'
FORWARDED=('pulse','state_start','state_refresh','idle')
ENCODED=('prediction','action','state_start','state_end','idle')  # the records EventEncoder reads
HAND=('sent','unsent','not_offered')  # what became of a hand line
LEASE_MS=1000  # the format-2 hello needs one; a v3 result carries no lease
MEMORY=4096  # sent (hand, boot_id, seq) kept against a second result
RETRY=1.  # seconds between attempts to reach the bridge, and before connecting again


class BridgeClosed(ValueError):
    """The bridge stream ended before its hello or inside a line: the service went away."""


class Stopped(KeyboardInterrupt):
    """SIGTERM (the launcher stopping or replacing this worker) ends socket mode as Ctrl+C does."""


def validate_hello(message):
    if not isinstance(message,dict) or message.get('type')!='hello' or message.get('format')!=1 or message.get('channels')!=CHANNELS:
        raise ValueError('Need bridge format 1 with exactly twelve ordered channels. Existing eleven-channel/v3 bridge is incompatible.')


def output_path(value):
    """--out: a new file, or an existing folder for a new file named from the UTC time and PID."""
    path=Path(value)
    if not path.is_dir():return path
    return path/'hand-worker-{}-{}.jsonl'.format(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),os.getpid())


class Recorder:
    def __init__(self,path,backend,synthetic,session_id=None):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.stream=path.open('x',encoding='utf-8',newline='\n')
        self.counts=Counter();self.devices={1:Counter(),2:Counter()}
        scope=dict(type='scope',backend=backend,synthetic=synthetic,
            gameplay_transport=False,final_gesture_accuracy_validated=False,
            timing_scope='worker processing only; source/host clocks are not synchronized')
        if session_id:scope.update(gameplay_transport=True,delivery=DELIVERY,forwarded_kinds=list(FORWARDED),
            logged_only_kinds=['state_end','reset'],session_id=session_id,hand_events='when_offered')
        self.write(scope)
    def write(self,record):
        self.stream.write(json.dumps(record,allow_nan=False)+'\n');self.stream.flush()
        self.counts[record['type']]+=1
        if record.get('device_id') in self.devices:self.devices[record['device_id']][record['type']]+=1
        if record['type'] in ('action','state_start','state_end','idle','hand_hello','hand_event','encoder_error','connection_lost',
                              'receive_error','waiting_for_bridge','connection_open','connection_end','stopped'):
            print(json.dumps(record),flush=True)
    def close(self,**extra):
        summary=dict(type='summary',status='failed' if self.counts['error'] else 'finished',
                     counts=dict(self.counts),hands={str(k):dict(v) for k,v in self.devices.items()},**extra)
        self.stream.write(json.dumps(summary)+'\n');self.stream.close();print(json.dumps(summary),flush=True)


class Delivery:
    """COMPAT: encoder events become bridge result lines; state_end and reset are only recorded.
    HAND EVENTS, when the bridge offers them: every encoder event is also a hand_event line,
    sent before its result line, after the connection's hand_hello.

    consume() takes every engine record in process_due order, on the inference thread, and
    sends each event's lines at once; the hand_event record follows the sends. The counts
    add up over every connection.
    """
    def __init__(self,hello,record):
        self.record=record
        self.counts={d:Counter(dict.fromkeys(('forwarded','collisions','encoder_errors','event_time_raised',
            'state_end_not_forwarded','reset_not_forwarded','unsent'),0)) for d in (1,2)}
        self.hands={d:Counter(dict.fromkeys(HAND,0)) for d in (1,2)};self.hellos=Counter(dict.fromkeys(HAND,0))
        self.reopen(hello)
    def reopen(self,hello):
        """A new bridge connection: a new encoder (worker_epoch), no state or sent memory from the last."""
        self.encoder=EventEncoder(hello);self.closed=False;self.failure=None;self.unsent=0
        self.offered=False;self.greeted=False;self.hand_unsent=0  # hand lines wait for the bridge's offer
        self.time={1:0.,2:0.}  # as EventEncoder: each hand's local time starts at 0, never decreases
        self.sent=OrderedDict()
    def offer(self,value):
        """The bridge hello's hand_events (the receiver, before the hello is answered): exactly 1 offers them."""
        self.offered=type(value) is int and value==1
    def greet(self,send):
        """Once per connection, before any other line: the hand_hello, when the bridge offered hand events."""
        if self.greeted:return
        self.greeted=True;hand='not_offered'
        if self.offered:hand='sent' if self._send(dict(type='hand_hello',hello=self.encoder.hello),send) else 'unsent'
        self.hellos[hand]+=1
        self.record(dict(type='hand_hello',worker_epoch=self.encoder.hello['worker_epoch'],hand_delivery=hand))
    def _send(self,line,send):
        """One line on this connection; False once a send has failed or the connection has ended."""
        if self.closed:return False
        try:send(line);return True
        except OSError as error:self.failure=error;self.closed=True;return False  # nothing more on this connection
    def consume(self,record,send):
        """One engine record; send(line) sends one bridge line (an OSError: the connection failed)."""
        device=record.get('device_id')
        if record.get('type') not in ENCODED or device not in (1,2):return
        now=record.get('elapsed_s')
        if type(now) in (int,float) and now<self.time[device]:
            # HandEngine.add stamps a reset with the sample's ARRIVAL time, which can precede a
            # prediction already handed over; the encoder needs non-decreasing time per hand.
            now=self.time[device];record=dict(record,elapsed_s=now);self.counts[device]['event_time_raised']+=1
        if type(now) in (int,float) and math.isfinite(now):self.time[device]=now
        try:events=self.encoder.consume(record)
        except ProtocolError as error:
            self.counts[device]['encoder_errors']+=1
            self.record(dict(type='encoder_error',device_id=device,record_type=record['type'],error=str(error)))
            events=self.encoder.reset('manual_reset',device=device)
        for event in events:self._event(device,event,send)
    def _event(self,device,event,send):
        kind,source=event['kind'],event['source'];counts=self.counts[device];hand='not_offered'
        if self.offered:
            self.greet(send)  # the hand_hello always goes first
            hand='sent' if self._send(dict(type='hand_event',event=event),send) else 'unsent'
            if hand=='unsent':self.hand_unsent+=1
        self.hands[device][hand]+=1
        if kind not in FORWARDED:delivery='not_forwarded';counts[kind+'_not_forwarded']+=1
        elif (device,source['boot_id'],source['seq']) in self.sent:delivery='collision';counts['collisions']+=1
        else:
            delivery='forwarded' if self._send(dict(type='result',gesture=event['gesture'],confidence=event['confidence'],
                device_id=device,boot_id=source['boot_id'],seq=source['seq']),send) else 'unsent'
            if delivery=='forwarded':
                self.sent[device,source['boot_id'],source['seq']]=None;counts['forwarded']+=1
                if len(self.sent)>MEMORY:self.sent.popitem(last=False)
            else:counts['unsent']+=1;self.unsent+=1
        self.record(dict(type='hand_event',device_id=device,kind=kind,gesture=event['gesture'],
            confidence=event['confidence'],event_id=event['event_id'],state_id=event['state_id'],
            reason=event['reason'],source=source,delivery=delivery,hand_delivery=hand))
    def summary(self):return {str(d):dict(c) for d,c in self.counts.items()}
    def hand_summary(self):return dict(hello=dict(self.hellos),hands={str(d):dict(c) for d,c in self.hands.items()})


def run_due(engine,now,record,deliver=None,clock=None,send=None):
    """Record every due engine record; with deliver, also hand it over (its result lines go to send)."""
    for event in engine.process_due(now,clock=clock):
        record(event)
        if deliver is not None:deliver.consume(event,send)


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


def observe_socket(path,engine,record,initialize=None,connection_factory=None,deliver=None):
    """Receiver continues while the single inference thread runs the backend.

    With deliver (a Delivery) the inference thread sends its lines on the bridge
    connection (when the bridge offered hand events, the hand_hello first, as soon
    as the backend is ready); without it nothing is ever sent. A failed send ends
    the loop with a connection_lost record. A stream that ends before its hello or
    inside a line raises BridgeClosed."""
    stop=threading.Event();ready=threading.Event();errors=[];lost=[]
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
                        if not hello:
                            validate_hello(message);hello=True
                            if deliver is not None:deliver.offer(message.get('hand_events'))
                            ready.set();continue
                        if not isinstance(message,dict) or message.get('type')!='sample':raise ValueError('Unexpected bridge message')
                        engine.add(message,time.monotonic())
                    if len(pending)>4096:raise ValueError('Bridge line too large')
                if pending.strip():raise BridgeClosed('Truncated bridge line')
                if not hello:raise BridgeClosed('Bridge closed before hello')
            except Exception as error:errors.append(error)
            finally:stop.set();engine.reset(time.monotonic())
        receiver=threading.Thread(target=receive,name='hand-receive',daemon=True);receiver.start()
        initialized=False
        def send(line):client.sendall((json.dumps(line,separators=(',',':'))+'\n').encode())
        try:
            while not stop.wait(.005):
                if not ready.is_set():continue
                if not initialized:
                    if initialize:initialize()
                    if deliver is not None:deliver.greet(send)
                    initialized=True
                run_due(engine,time.monotonic(),record,deliver,time.monotonic,send)
                if deliver is not None and deliver.failure is not None:
                    error=deliver.failure;lost.append(error)
                    record(dict(type='connection_lost',error_type=type(error).__name__,message=str(error),
                                results_unsent=deliver.unsent,
                                **(dict(hand_events_unsent=deliver.hand_unsent) if deliver.offered else {})))
                    break
        finally:
            stop.set()
            try:client.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            receiver.join(2)
            if deliver is not None:
                deliver.closed=True  # the connection is closed: nothing more is sent
                if ready.is_set():deliver.greet(send)  # a hello never answered is recorded as such
            run_due(engine,time.monotonic(),record,deliver,time.monotonic,send)
        # After a lost connection the receiver's failure is its consequence, not a new fault.
        if errors and not lost:raise errors[0]
        for error in errors:record(dict(type='receive_error',error_type=type(error).__name__,message=str(error)))


def wait_for_bridge(path,record,retry):
    """Connect, trying again every retry seconds while the service's socket is absent or refused."""
    waiting=False
    while True:
        try:return connect_bridge(path)
        except (FileNotFoundError,ConnectionRefusedError) as error:
            if not waiting:record(dict(type='waiting_for_bridge',socket=path,error_type=type(error).__name__));waiting=True
        time.sleep(retry)


def serve_socket(path,engine,record,initialize=None,deliver=None,hello=None,retry=RETRY):
    """Socket mode until SIGTERM or Ctrl+C: connect, observe (and deliver), connect again.

    A connection ends at the end of its stream, a failed send, a broken stream or a reset
    connection; observe_socket has then reset both hands. With deliver, each connection
    gets a new encoder from hello() (a new worker_epoch). Other errors end the worker."""
    connection=0
    while True:
        client=wait_for_bridge(path,record,retry);connection+=1
        if deliver is not None:deliver.reopen(hello())
        record(dict(type='connection_open',connection=connection,
                    **(dict(worker_epoch=deliver.encoder.hello['worker_epoch']) if deliver is not None else {})))
        try:observe_socket(path,engine,record,initialize,lambda path:client,deliver)
        except (BridgeClosed,ConnectionError) as error:
            record(dict(type='receive_error',error_type=type(error).__name__,message=str(error)))
        record(dict(type='connection_end',connection=connection,reconnect_in_s=retry))
        time.sleep(retry)


def stop_on_sigterm():
    """Make SIGTERM raise Stopped where this Python and thread allow it; returns the previous handler."""
    def stop(signum,frame):
        signal.signal(signal.SIGTERM,signal.SIG_IGN)  # a second SIGTERM must not cut the summary short
        raise Stopped('SIGTERM')
    try:return signal.signal(signal.SIGTERM,stop)
    except (AttributeError,ValueError):return None  # no SIGTERM here, or not the main thread


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle',required=True);p.add_argument('--allow-synthetic',action='store_true')
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--replay');source.add_argument('--socket')
    p.add_argument('--observe-only',action='store_true',help='socket mode: record only, send no results')
    p.add_argument('--session-id',default='ltc-comms',
                   help='session of the hand-event hello; sent only when the bridge offers hand events')
    p.add_argument('--backend',choices=('numpy','fpga'),default='numpy')
    p.add_argument('--bit');p.add_argument('--ip-name',default='gesture_cnn_hand_u2_cache_0')
    p.add_argument('--threshold',type=float,default=.7);p.add_argument('--confirmations',type=int,default=3)
    p.add_argument('--interval',type=float,default=.2)
    p.add_argument('--out',required=True,help='a new JSONL file, or an existing folder for a new file')
    a=p.parse_args(argv)
    if a.socket and not hasattr(socket,'AF_UNIX'):
        p.error('This Python build has no Unix sockets. Use --replay on Windows; run --socket on Ultra96/Linux.')
    if a.backend=='fpga' and not a.bit:p.error('FPGA mode requires the NEW matching --bit and .hwh')
    if a.backend=='numpy' and a.bit:p.error('--bit is only meaningful with --backend fpga')
    if a.observe_only and not a.socket:p.error('--observe-only is for --socket; --replay never sends results')
    hello=None
    if a.socket and not a.observe_only:
        hello=lambda:make_hello(a.session_id,secrets.token_hex(16),LEASE_MS,a.threshold,a.confirmations)
        try:hello()  # each connection gets a new one; refuse the settings before reading the bundle
        except ValueError as error:p.error('Compat delivery refuses these settings: '+str(error))
    bundle=ModelBundle(a.bundle,a.allow_synthetic)
    # Validate settings before allocating buffers or downloading an overlay.
    engine=HandEngine(None,a.threshold,a.confirmations,a.interval)
    recorder=Recorder(output_path(a.out),a.backend,bundle.synthetic,a.session_id if hello else None);backend=None
    delivery=Delivery(hello(),recorder.write) if hello else None
    recorder.write(dict(type='bundle_verified',manifest_sha256=digest(bundle.folder/'manifest.json'),
                        model_parameter_sha256={str(d):bundle.models[d]['hash'] for d in (1,2)}))
    def initialize():
        nonlocal backend
        if backend is not None:return  # one backend (one overlay download) for every bridge connection
        backend=(NumpyHandBackend(bundle) if a.backend=='numpy' else PynqHandBackend(bundle,a.bit,a.ip_name))
        engine.backend=backend
        if a.backend=='fpga':recorder.write(dict(type='hardware_artifacts',**backend.artifacts))
    previous=None
    try:
        if a.replay:replay(a.replay,engine,recorder.write,initialize)
        else:
            previous=stop_on_sigterm()
            try:serve_socket(a.socket,engine,recorder.write,initialize,delivery,hello,RETRY)
            except KeyboardInterrupt as stopped:recorder.write(dict(type='stopped',signal=str(stopped) or 'SIGINT'))
    except BaseException as error:
        recorder.write(dict(type='error',error_type=type(error).__name__,message=str(error)))
        raise
    finally:
        if previous is not None:signal.signal(signal.SIGTERM,previous)
        try:
            if backend:backend.close()
        finally:recorder.close(**(dict(delivery=delivery.summary(),hand_delivery=delivery.hand_summary()) if delivery else {}))


if __name__=='__main__':main()
