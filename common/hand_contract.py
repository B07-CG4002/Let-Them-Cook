"""OPT-IN reference contract: sensor v4 and independent-hand events.

This does not change common.sensor or ultra96.protocol. Existing production
endpoints reject these new messages until their adapters are migrated together.
"""
from dataclasses import dataclass
import math
import re
import struct

from common.wire import ProtocolError

CHANNELS=('ax','ay','az','gx','gy','gz','yaw','pitch','roll','flex1','flex2','flex3')
SCALES=(10000,10000,10000,100,100,100,100,100,100,10000,10000,10000)
LABELS=('PICKUP','DROP','CHOP','WASH','THROW','POURING','IDLE')
PULSES={'PICKUP','DROP','THROW'}
ONGOING={'CHOP','WASH','POURING'}
PACKET=struct.Struct('<2sBBIII12h')
MAX_EVENT_SEQ=2**53-1  # exactly representable by JavaScript numbers
_EPOCH=re.compile(r'[0-9a-f]{32}')
_SENSOR_FIELDS={'v','type','session_id','device_id','boot_id','seq','uptime_ms','values','request_id'}
_HELLO_FIELDS={'type','format','sensor_version','channels','class_names','event_format',
               'session_id','worker_epoch','lease_ms','confidence_threshold','confirmations'}
_EVENT_FIELDS={'v','type','event_format','session_id','worker_epoch','device_id','event_seq',
               'event_id','kind','gesture','confidence','source','state_id','lease_ms','reason'}
END_REASONS={'confirmed_transition','prediction_timeout','source_reset','connection_reset',
             'worker_disconnect','manual_reset'}


def integer(value,low,high):return type(value) is int and low<=value<=high


def confidence(value):
    return type(value) in (int,float) and 0<=value<=1 and math.isfinite(value)


def session(value):
    if (not isinstance(value,str) or not 1<=len(value)<=128 or
            any(ord(c)<32 or 0xD800<=ord(c)<=0xDFFF for c in value)):
        raise ProtocolError('Invalid session_id')


def raw_values(values):
    if not isinstance(values,(tuple,list)) or len(values)!=12 or any(not integer(v,-32768,32767) for v in values):
        raise ProtocolError('Need exactly twelve int16 sensor values')


@dataclass(frozen=True)
class SensorV4:
    device_id: int
    boot_id: int
    seq: int
    uptime_ms: int
    values: tuple

    def __post_init__(self):
        if not integer(self.device_id,1,2):raise ProtocolError('Invalid device_id')
        for value in (self.boot_id,self.seq,self.uptime_ms):
            if not integer(value,0,2**32-1):raise ProtocolError('Invalid sensor uint32')
        raw_values(self.values);object.__setattr__(self,'values',tuple(self.values))

    def encode(self):return PACKET.pack(b'LC',4,self.device_id,self.boot_id,self.seq,self.uptime_ms,*self.values)

    def message(self,session_id,request_id=None):
        result=dict(v=4,type='SENSOR_BATCH',session_id=session_id,device_id=self.device_id,
                    boot_id=self.boot_id,seq=self.seq,uptime_ms=self.uptime_ms,
                    values=list(self.values),request_id=request_id)
        return validate_sensor(result)


def decode_packet(data):
    if not isinstance(data,(bytes,bytearray,memoryview)) or len(data)!=40:
        raise ProtocolError('Sensor v4 packets are exactly 40 bytes; no padding/truncation')
    magic,version,device,boot,seq,uptime,*values=PACKET.unpack(data)
    if magic!=b'LC' or version!=4:raise ProtocolError('Expected LC sensor protocol v4')
    return SensorV4(device,boot,seq,uptime,tuple(values))


def decode_notification(data):
    if not isinstance(data,(bytes,bytearray,memoryview)) or len(data)%40 or not 1<=len(data)//40<=6:
        raise ProtocolError('Notification must contain 1 to 6 whole v4 packets')
    return tuple(decode_packet(data[i:i+40]) for i in range(0,len(data),40))


def validate_sensor(message,session_id=None):
    if not isinstance(message,dict) or set(message)!=_SENSOR_FIELDS:
        raise ProtocolError('Unexpected v4 sensor fields')
    if type(message['v']) is not int or message['v']!=4 or message['type']!='SENSOR_BATCH':
        raise ProtocolError('Expected SENSOR_BATCH v4')
    session(message['session_id'])
    if session_id is not None and message['session_id']!=session_id:raise ProtocolError('Wrong sensor session')
    SensorV4(message['device_id'],message['boot_id'],message['seq'],message['uptime_ms'],message['values'])
    request=message['request_id']
    if request is not None and (not integer(request,1,2**32-1) or request!=message['seq']):
        raise ProtocolError('Command request_id must be uint32 equal to seq')
    return message


def to_bridge_sample(message,session_id=None):
    validate_sensor(message,session_id)
    if message['request_id'] is not None:raise ProtocolError('Commands do not enter AI sample processing')
    return dict(type='sample',device_id=message['device_id'],boot_id=message['boot_id'],
                seq=message['seq'],uptime_ms=message['uptime_ms'],
                values=[v/s for v,s in zip(message['values'],SCALES)])


def make_hello(session_id,worker_epoch,lease_ms=1000,threshold=.70,confirmations=3):
    return validate_hello(dict(type='hello',format=2,sensor_version=4,channels=list(CHANNELS),
        class_names=list(LABELS),event_format=1,session_id=session_id,worker_epoch=worker_epoch,
        lease_ms=lease_ms,confidence_threshold=threshold,confirmations=confirmations))


def validate_hello(message):
    if not isinstance(message,dict) or set(message)!=_HELLO_FIELDS:
        raise ProtocolError('Need negotiated hand-event hello format 2')
    if (message['type']!='hello' or type(message['format']) is not int or message['format']!=2 or
            type(message['sensor_version']) is not int or message['sensor_version']!=4 or
            type(message['event_format']) is not int or message['event_format']!=1 or
            message['channels']!=list(CHANNELS) or message['class_names']!=list(LABELS)):
        raise ProtocolError('Incompatible channel/class/sensor/event contract')
    session(message['session_id'])
    if not isinstance(message['worker_epoch'],str) or not _EPOCH.fullmatch(message['worker_epoch']):
        raise ProtocolError('worker_epoch must be a fresh 32-hex connection identifier')
    if not integer(message['lease_ms'],100,2000) or not confidence(message['confidence_threshold']) or not integer(message['confirmations'],1,10):
        raise ProtocolError('Invalid negotiated confirmation/lease settings')
    return message


def event_id(epoch,device,sequence):return '{}:{}:{}'.format(epoch,device,sequence)


def _source(source,optional=False):
    if source is None and optional:return
    if not isinstance(source,dict) or set(source)!={'boot_id','seq','uptime_ms'} or any(not integer(v,0,2**32-1) for v in source.values()):
        raise ProtocolError('Invalid source identity')


def validate_event(message,hello):
    validate_hello(hello)
    if not isinstance(message,dict) or set(message)!=_EVENT_FIELDS:raise ProtocolError('Unexpected hand event fields')
    if (type(message['v']) is not int or message['v']!=4 or message['type']!='HAND_EVENT' or
            type(message['event_format']) is not int or message['event_format']!=1):
        raise ProtocolError('Expected HAND_EVENT v4/event_format 1')
    if message['session_id']!=hello['session_id'] or message['worker_epoch']!=hello['worker_epoch']:
        raise ProtocolError('Wrong session or obsolete worker connection')
    device,sequence=message['device_id'],message['event_seq']
    if not integer(device,1,2) or not integer(sequence,1,MAX_EVENT_SEQ):raise ProtocolError('Invalid event identity')
    if message['event_id']!=event_id(hello['worker_epoch'],device,sequence):raise ProtocolError('event_id mismatch')
    kind,gesture=message['kind'],message['gesture']
    if kind not in ('pulse','state_start','state_refresh','state_end','idle','reset'):
        raise ProtocolError('Unknown event kind')
    if kind!='reset' and not isinstance(gesture,str):raise ProtocolError('Gesture must be a string')
    if kind in ('pulse','state_start','state_refresh','idle'):
        if not confidence(message['confidence']) or message['confidence']<=hello['confidence_threshold']:
            raise ProtocolError('Accepted predictions require confidence strictly above threshold')
        if message['reason']!='confirmed':raise ProtocolError('Invalid prediction reason')
    elif message['confidence'] is not None or not isinstance(message['reason'],str) or message['reason'] not in END_REASONS:
        raise ProtocolError('End/reset is a lifecycle event, not an IDLE prediction')
    _source(message['source'],optional=kind=='reset')
    if (kind=='pulse' and gesture not in PULSES or
            kind in ('state_start','state_refresh','state_end') and gesture not in ONGOING or
            kind=='idle' and gesture!='IDLE' or kind=='reset' and gesture is not None):
        raise ProtocolError('Gesture is incompatible with event kind')
    if kind in ('state_start','state_refresh','state_end'):
        state_id=message['state_id'];prefix=hello['worker_epoch']+':'+str(device)+':'
        suffix=state_id[len(prefix):] if isinstance(state_id,str) and state_id.startswith(prefix) else ''
        if (not suffix.isascii() or not suffix.isdecimal() or len(suffix)>16 or
                not 1<=int(suffix)<=sequence or str(int(suffix))!=suffix):
            raise ProtocolError('Invalid state identity')
        if kind=='state_start' and state_id!=message['event_id']:raise ProtocolError('State must start with its own event ID')
    elif message['state_id'] is not None:raise ProtocolError('Non-state event cannot name a state')
    lease=message['lease_ms']
    if type(lease) is not int or lease!=(hello['lease_ms'] if kind in ('state_start','state_refresh') else 0):
        raise ProtocolError('Invalid state lease')
    return message
