"""Opt-in event admission helpers for the v4 service migration.

Use this in the event-loop thread before publishing complete HAND_EVENT
envelopes. This module opens no service and changes no production imports.
"""
from collections import OrderedDict
import math

from common.hand_contract import ProtocolError,validate_sensor,validate_event,validate_hello


class HandAdmission:
    def __init__(self,hello,capacity=512,max_namespaces=128,max_prediction_age=.5):
        self.hello=validate_hello(hello)
        if type(capacity) is not int or capacity<1 or type(max_namespaces) is not int or max_namespaces<2:
            raise ValueError('Invalid admission limits')
        if type(max_prediction_age) not in (int,float) or not math.isfinite(max_prediction_age) or max_prediction_age<=0:
            raise ValueError('Invalid prediction freshness')
        self.capacity=capacity;self.max_namespaces=max_namespaces;self.max_prediction_age=max_prediction_age
        self.samples={1:OrderedDict(),2:OrderedDict()};self.current={1:None,2:None};self.namespaces=set()

    def accepted_sample(self,message,now):
        validate_sensor(message,self.hello['session_id'])
        if message['request_id'] is not None:raise ProtocolError('Commands must not enter the model ledger')
        if type(now) not in (int,float) or not math.isfinite(now) or now<0:raise ValueError('Invalid server clock')
        device,boot,seq=(message[k] for k in ('device_id','boot_id','seq'))
        key=(boot,seq);signature=(message['uptime_ms'],tuple(message['values']))
        entries=self.samples[device]
        if key in entries:
            if entries[key][0]!=signature:raise ProtocolError('Source identity reused with changed payload')
            return False  # retransmission does NOT refresh the source arrival time
        previous=self.current[device]
        if previous:
            old_boot,old_seq=previous
            if boot==old_boot:
                delta=(seq-old_seq)%2**32
                if delta==0 or delta>=2**31:return False
            elif (device,boot) in self.namespaces:return False
        if (device,boot) not in self.namespaces:
            if len(self.namespaces)>=self.max_namespaces:raise ProtocolError('Source namespace limit reached')
            self.namespaces.add((device,boot))
        self.current[device]=(boot,seq);entries[key]=(signature,float(now))
        while len(entries)>self.capacity:entries.popitem(last=False)
        return True

    def event(self,message,now):
        validate_event(message,self.hello)
        if type(now) not in (int,float) or not math.isfinite(now) or now<0:raise ValueError('Invalid server clock')
        source=message['source'];kind=message['kind'];device=message['device_id']
        if source is None:return message  # schema permits this only for a lifecycle reset
        entry=self.samples[device].get((source['boot_id'],source['seq']))
        if entry is None or entry[0][0]!=source['uptime_ms']:
            raise ProtocolError('Event source is not an exact accepted sample')
        if kind in ('pulse','state_start','state_refresh','idle'):
            current=self.current[device]
            if current is None or source['boot_id']!=current[0]:raise ProtocolError('Prediction belongs to a retired boot')
            if not 0<=now-entry[1]<=self.max_prediction_age:raise ProtocolError('Prediction source is stale at server')
        # Lifecycle end may legitimately refer to an older accepted source.
        # state_id checks at the consumer prevent it clearing an unrelated state.
        return message
