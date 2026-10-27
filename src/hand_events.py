"""Opt-in reference encoder/consumer for LOCAL hand action records.

No socket, TLS service, phone or gameplay is opened or modified here. A future
adapter must negotiate hello format 2 and preserve every event field.
"""
import copy
import math

from common.hand_contract import (ProtocolError,MAX_EVENT_SEQ,ONGOING,event_id,
                                  validate_hello,validate_event)


class EventEncoder:
    def __init__(self,hello):
        self.hello=copy.deepcopy(validate_hello(hello));self.sequences={1:0,2:0}
        self.active={1:None,2:None};self.source={1:None,2:None};self.last_refresh={1:None,2:None}
        self.last_prediction={1:None,2:None};self.last_time={1:0.,2:0.}

    def _event(self,device,kind,gesture=None,confidence=None,state_id=None,reason='confirmed'):
        if type(device) is not int or device not in (1,2):raise ProtocolError('Invalid hand')
        sequence=self.sequences[device]+1
        if sequence>MAX_EVENT_SEQ:raise OverflowError('Negotiate a new worker_epoch before event counter reuse')
        identity=event_id(self.hello['worker_epoch'],device,sequence)
        message=dict(v=4,type='HAND_EVENT',event_format=1,session_id=self.hello['session_id'],
            worker_epoch=self.hello['worker_epoch'],device_id=device,event_seq=sequence,event_id=identity,
            kind=kind,gesture=gesture,confidence=confidence,source=copy.deepcopy(self.source[device]),
            state_id=identity if kind=='state_start' else state_id,
            lease_ms=self.hello['lease_ms'] if kind in ('state_start','state_refresh') else 0,reason=reason)
        validate_event(message,self.hello);self.sequences[device]=sequence
        return message

    def consume(self,record):
        kind=record.get('type')
        if kind not in ('prediction','action','state_start','state_end','idle'):return []
        device=record.get('device_id')
        if type(device) is not int or device not in (1,2):raise ProtocolError('Invalid local hand')
        now=record.get('elapsed_s')
        if type(now) not in (int,float) or not math.isfinite(now) or now<self.last_time[device]:raise ProtocolError('Invalid local event time')
        self.last_time[device]=now
        if all(k in record for k in ('boot_id','seq','uptime_ms')):
            self.source[device]={k:record[k] for k in ('boot_id','seq','uptime_ms')}
        gesture=record.get('gesture');value=record.get('confidence')
        if kind=='prediction':
            identity=tuple(self.source[device][k] for k in ('boot_id','seq')) if self.source[device] else None
            if identity is None:raise ProtocolError('Prediction requires source identity')
            previous=self.last_prediction[device]
            if previous and identity[0]==previous[0] and ((identity[1]-previous[1])%2**32==0 or
                                                       (identity[1]-previous[1])%2**32>=2**31):return []
            self.last_prediction[device]=identity
            active=self.active[device]
            if (active and gesture==active['gesture'] and type(value) in (int,float) and
                    value>self.hello['confidence_threshold'] and
                    now-self.last_refresh[device]>=.2-1e-9):
                message=self._event(device,'state_refresh',gesture,value,active['state_id'])
                self.last_refresh[device]=now;return [message]
            return []
        if kind=='state_end':
            active=self.active[device]
            if active is None:return []
            if active['gesture']!=gesture:raise ProtocolError('Local state end does not match active state')
            message=self._event(device,'state_end',gesture,state_id=active['state_id'],reason=record['reason'])
            self.active[device]=None;return [message]
        if kind=='state_start':
            if self.active[device] is not None:raise ProtocolError('End the previous local state before starting another')
            message=self._event(device,'state_start',gesture,value)
            self.active[device]=dict(state_id=message['state_id'],gesture=gesture)
            self.last_refresh[device]=now;return [message]
        message=self._event(device,'pulse' if kind=='action' else 'idle',gesture,value)
        self.active[device]=None
        return [message]

    def reset(self,reason='connection_reset',device=None):
        result=[]
        for hand in (1,2) if device is None else (device,):
            message=self._event(hand,'reset',reason=reason);self.active[hand]=None
            self.source[hand]=None;self.last_refresh[hand]=None;result.append(message)
            self.last_prediction[hand]=None
        return result


class EventConsumer:
    """Reference state only; accepted pulses are requests, not game execution.

    Ordered counters suppress duplicate/out-of-order events within a negotiated
    worker epoch. State leases use this consumer's clock. Refreshes never create
    or resurrect an unknown/expired state. Reconnect must resynchronize states.
    """
    def __init__(self,hello):
        self.hello=copy.deepcopy(validate_hello(hello));self.high_water={1:0,2:0}
        self.active={1:None,2:None};self.pulses=[];self.ends=[];self.now=0.

    def reconnect(self,hello,now):
        validate_hello(hello)
        if hello['worker_epoch']==self.hello['worker_epoch']:raise ProtocolError('Reconnect requires a fresh worker_epoch')
        self._time(now)
        for device in (1,2):self._end(device,'connection_reset')
        self.hello=copy.deepcopy(hello);self.high_water={1:0,2:0}

    def _time(self,now):
        if type(now) not in (int,float) or not math.isfinite(now) or now<self.now:raise ProtocolError('Consumer clock must be finite and monotonic')
        self.now=now

    def _end(self,device,reason):
        if self.active[device] is not None:
            self.ends.append(dict(device_id=device,state_id=self.active[device]['state_id'],reason=reason))
            self.active[device]=None

    def expire(self,now):
        self._time(now)
        for device in (1,2):
            if self.active[device] and now>=self.active[device]['deadline']:
                self._end(device,'lease_expired')

    def apply(self,message,now):
        validate_event(message,self.hello);self.expire(now)
        device,sequence=message['device_id'],message['event_seq']
        if sequence==self.high_water[device]:return 'duplicate'
        if sequence<self.high_water[device]:return 'out_of_order'
        self.high_water[device]=sequence
        kind=message['kind'];active=self.active[device]
        if kind=='pulse':
            self._end(device,'confirmed_transition');self.pulses.append(copy.deepcopy(message));return 'pulse'
        if kind=='state_start':
            self._end(device,'confirmed_transition')
            self.active[device]=dict(state_id=message['state_id'],gesture=message['gesture'],
                                     deadline=now+message['lease_ms']/1000.)
            return 'state_started'
        if kind=='state_refresh':
            if active is None or active['state_id']!=message['state_id'] or active['gesture']!=message['gesture']:
                return 'orphan_refresh'
            active['deadline']=now+message['lease_ms']/1000.;return 'refreshed'
        if kind=='state_end':
            if active is None or active['state_id']!=message['state_id']:return 'orphan_end'
            self._end(device,message['reason']);return 'state_ended'
        self._end(device,'confirmed_idle' if kind=='idle' else message['reason'])
        return kind
