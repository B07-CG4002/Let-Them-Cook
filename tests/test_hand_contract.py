import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

from common.hand_contract import (SensorV4,decode_packet,decode_notification,to_bridge_sample,
    validate_sensor,validate_event,make_hello,ProtocolError,CHANNELS,MAX_EVENT_SEQ,event_id)
from src.hand_events import EventEncoder,EventConsumer
from ultra96.hand_admission import HandAdmission
from tools.hand_contract_demo import run,roundtrip


def hello(epoch='1234567890abcdef1234567890abcdef'):
    return make_hello('test-session',epoch)


def record(kind='state_start',gesture='CHOP',device=1,time=0.,seq=1,confidence=.95,**extra):
    result=dict(type=kind,gesture=gesture,device_id=device,elapsed_s=time,
                boot_id=10+device,seq=seq,uptime_ms=seq*20)
    if kind not in ('state_end',):result['confidence']=confidence
    result.update(extra);return result


class SensorTests(unittest.TestCase):
    def test_v4_bytes_match_existing_recorder_codec(self):
        from src.hand_ai_flex3 import sensor as recorder
        packet=SensorV4(2,123,456,789,tuple(range(12)))
        self.assertEqual(packet.encode(),recorder.encode_packet(recorder.SensorPacket(2,123,456,789,tuple(range(12)))))
        self.assertEqual(len(packet.encode()),40)
        self.assertEqual(decode_packet(packet.encode()),packet)

    def test_notifications_decode_one_to_six_whole_packets(self):
        packet=SensorV4(1,11,1,20,(0,)*12)
        self.assertEqual(decode_notification(packet.encode()*6),(packet,)*6)
        for raw in (b'',packet.encode()[:-1],packet.encode()*7):
            with self.assertRaises(ProtocolError):decode_notification(raw)

    def test_v3_is_refused_without_padding(self):
        old=struct.pack('<2sBBIII11h',b'LC',3,1,11,1,20,*([0]*11))
        with self.assertRaises(ProtocolError):decode_packet(old)
        changed=bytearray(SensorV4(1,11,1,20,(0,)*12).encode());changed[2]=3
        with self.assertRaises(ProtocolError):decode_packet(changed)

    def test_scale_conversion_occurs_once_and_preserves_thumb(self):
        packet=SensorV4(2,12,1,20,(10000,-10000,5000,100,-200,300,1000,-2000,3000,4000,5000,6000))
        sample=to_bridge_sample(packet.message('test-session'))
        self.assertEqual(sample['values'],[1.,-1.,.5,1.,-2.,3.,10.,-20.,30.,.4,.5,.6])
        self.assertEqual(len(sample['values']),12)

    def test_sensor_strict_fields_and_integer_types(self):
        original=SensorV4(1,11,1,20,(0,)*12).message('test-session')
        for field,value in (('v',3),('v',True),('seq',-1),('values',[0]*11),('device_id',True)):
            message=copy.deepcopy(original);message[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ProtocolError):validate_sensor(message)
        original['extra']=1
        with self.assertRaises(ProtocolError):validate_sensor(original)

    def test_commands_are_not_forwarded_to_ai(self):
        packet=SensorV4(1,11,1,20,(0,)*12)
        with self.assertRaisesRegex(ProtocolError,'Commands'):to_bridge_sample(packet.message('test-session',1))


class EventTests(unittest.TestCase):
    def setUp(self):self.hello=hello();self.encoder=EventEncoder(self.hello);self.consumer=EventConsumer(self.hello)

    def test_end_and_start_from_same_sample_have_distinct_ids(self):
        start=self.encoder.consume(record())[0];self.consumer.apply(start,0.)
        end=self.encoder.consume(record('state_end',time=.2,seq=2,reason='confirmed_transition'))[0]
        next_start=self.encoder.consume(record('state_start','POURING',time=.2,seq=2))[0]
        self.assertEqual(end['source'],next_start['source'])
        self.assertNotEqual(end['event_id'],next_start['event_id'])
        self.consumer.apply(end,.2);self.consumer.apply(next_start,.2)
        self.assertEqual(self.consumer.active[1]['gesture'],'POURING')

    def test_duplicate_pulse_is_applied_once(self):
        pulse=self.encoder.consume(record('action','PICKUP'))[0]
        self.assertEqual(self.consumer.apply(pulse,0.),'pulse')
        self.assertEqual(self.consumer.apply(pulse,.1),'duplicate')
        self.assertEqual(len(self.consumer.pulses),1)

    def test_lease_expiry_stops_state_when_end_is_lost(self):
        start=self.encoder.consume(record())[0];self.consumer.apply(start,0.)
        self.consumer.expire(1.)
        self.assertIsNone(self.consumer.active[1])
        self.assertEqual(self.consumer.ends[-1]['reason'],'lease_expired')

    def test_duplicate_refresh_does_not_extend_lease(self):
        self.consumer.apply(self.encoder.consume(record())[0],0.)
        refresh=self.encoder.consume(record('prediction',time=.2,seq=2))[0]
        self.consumer.apply(refresh,.2);self.consumer.apply(refresh,.9)
        self.consumer.expire(1.21);self.assertIsNone(self.consumer.active[1])

    def test_old_prediction_does_not_generate_new_refresh(self):
        self.encoder.consume(record())
        self.encoder.consume(record('prediction',time=.2,seq=2))
        self.assertEqual(self.encoder.consume(record('prediction',time=.5,seq=2)),[])

    def test_refresh_never_resurrects_expired_or_unknown_state(self):
        self.consumer.apply(self.encoder.consume(record())[0],0.)
        refresh=self.encoder.consume(record('prediction',time=.2,seq=2))[0]
        self.consumer.expire(1.)
        self.assertEqual(self.consumer.apply(refresh,1.1),'orphan_refresh')
        self.assertIsNone(self.consumer.active[1])

    def test_hands_are_independent(self):
        self.consumer.apply(self.encoder.consume(record())[0],0.)
        self.consumer.apply(self.encoder.consume(record('action','PICKUP',device=2))[0],0.)
        self.assertEqual(self.consumer.active[1]['gesture'],'CHOP')
        self.assertIsNone(self.consumer.active[2])
        self.assertEqual(self.consumer.pulses[0]['device_id'],2)

    def test_old_connection_cannot_send_events_after_reconnect(self):
        old=self.encoder.consume(record())[0];self.consumer.apply(old,0.)
        self.consumer.reconnect(hello('f'*32),.1)
        with self.assertRaisesRegex(ProtocolError,'obsolete'):self.consumer.apply(old,.2)
        self.assertIsNone(self.consumer.active[1])

    def test_low_confidence_does_not_become_idle(self):
        self.assertEqual(self.encoder.consume(record('prediction','IDLE',confidence=.2)),[])
        with self.assertRaisesRegex(ProtocolError,'strictly above'):
            self.encoder.consume(record('idle','IDLE',confidence=.70))

    def test_timeout_end_has_no_invented_confidence_or_idle(self):
        start=self.encoder.consume(record())[0]
        end=self.encoder.consume(dict(type='state_end',device_id=1,elapsed_s=.7,gesture='CHOP',reason='prediction_timeout'))[0]
        self.assertIsNone(end['confidence']);self.assertEqual(end['source'],start['source'])
        self.assertEqual(end['gesture'],'CHOP');self.assertEqual(end['lease_ms'],0)

    def test_state_id_protects_new_state_from_an_old_end(self):
        old=self.encoder.consume(record())[0]
        self.encoder.consume(record('state_end',time=.2,seq=2,reason='confirmed_transition'))
        new=self.encoder.consume(record('state_start','POURING',time=.2,seq=2))[0]
        self.consumer.apply(old,0.);self.consumer.apply(new,.2)
        late=copy.deepcopy(old)
        late.update(event_seq=4,event_id=event_id(self.hello['worker_epoch'],1,4),kind='state_end',
                    confidence=None,lease_ms=0,reason='prediction_timeout')
        self.assertEqual(self.consumer.apply(late,.3),'orphan_end')
        self.assertEqual(self.consumer.active[1]['gesture'],'POURING')

    def test_malformed_event_fields_are_refused(self):
        original=self.encoder.consume(record())[0]
        for field,value in (('event_id','wrong'),('event_seq',True),('event_seq',2**53),
                            ('gesture',[]),('confidence',float('nan')),('lease_ms',True),('state_id','old')):
            bad=copy.deepcopy(original);bad[field]=value
            with self.subTest(field=field),self.assertRaises(ProtocolError):validate_event(bad,self.hello)

    def test_event_counter_must_not_wrap(self):
        self.encoder.sequences[1]=MAX_EVENT_SEQ
        with self.assertRaises(OverflowError):self.encoder.consume(record('action','PICKUP'))

    def test_connection_reset_is_explicit_and_never_idle(self):
        self.consumer.apply(self.encoder.consume(record())[0],0.)
        events=self.encoder.reset()
        for message in events:self.consumer.apply(message,.2)
        self.assertTrue(all(message['gesture'] is None for message in events))
        self.assertTrue(all(value is None for value in self.consumer.active.values()))


class AdmissionTests(unittest.TestCase):
    def setUp(self):self.hello=hello();self.gate=HandAdmission(self.hello);self.encoder=EventEncoder(self.hello)
    def sample(self,seq=1,boot=11):return SensorV4(1,boot,seq,seq*20,(0,)*12).message('test-session')

    def test_exact_source_required_not_just_below_high_water(self):
        self.gate.accepted_sample(self.sample(2),0.)
        pulse=self.encoder.consume(record('action','PICKUP',seq=1))[0]
        with self.assertRaisesRegex(ProtocolError,'exact accepted'):self.gate.event(pulse,.1)

    def test_duplicate_sample_does_not_refresh_prediction_age(self):
        sample=self.sample();self.gate.accepted_sample(sample,0.)
        self.assertFalse(self.gate.accepted_sample(sample,.4))
        pulse=self.encoder.consume(record('action','PICKUP'))[0]
        with self.assertRaisesRegex(ProtocolError,'stale'):self.gate.event(pulse,.6)

    def test_changed_payload_under_same_source_id_is_refused(self):
        sample=self.sample();self.gate.accepted_sample(sample,0.)
        sample['values'][0]=1
        with self.assertRaisesRegex(ProtocolError,'changed payload'):self.gate.accepted_sample(sample,.1)

    def test_retired_boot_cannot_become_current_again(self):
        self.gate.accepted_sample(self.sample(),0.)
        self.gate.accepted_sample(self.sample(0,99),.1)
        self.assertFalse(self.gate.accepted_sample(self.sample(2),.2))
        pulse=self.encoder.consume(record('action','PICKUP'))[0]
        with self.assertRaisesRegex(ProtocolError,'retired boot'):self.gate.event(pulse,.2)

    def test_lifecycle_end_may_reference_older_accepted_sample(self):
        self.gate.accepted_sample(self.sample(),0.)
        start=self.encoder.consume(record())[0];self.gate.event(start,.1)
        end=self.encoder.consume(dict(type='state_end',device_id=1,elapsed_s=.7,gesture='CHOP',reason='prediction_timeout'))[0]
        self.assertEqual(self.gate.event(end,.7),end)

    def test_bounded_sample_ledger_and_uint32_wrap(self):
        gate=HandAdmission(self.hello,capacity=2)
        for i,seq in enumerate((2**32-1,0,1)):
            sample=SensorV4(1,11,seq,i*20,(0,)*12).message('test-session')
            self.assertTrue(gate.accepted_sample(sample,i*.02))
        self.assertEqual(len(gate.samples[1]),2)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_length_prefixed_framing_preserves_event_fields(self):
        message=EventEncoder(hello()).consume(record())[0]
        self.assertEqual(await roundtrip(message),message)

    async def test_demo_handles_duplicate_pulse_and_missing_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'demo'
            with contextlib.redirect_stdout(io.StringIO()):summary=await run(out)
            self.assertEqual(summary['sensor_frames'],442)
            self.assertEqual(summary['accepted_pulses'],1)
            self.assertEqual(summary['same_source_distinct_event_ids'],2)
            self.assertEqual(summary['lease_expirations'],1)
            self.assertFalse(summary['gameplay_executed'])
            with self.assertRaises(FileExistsError):await run(out)


if __name__=='__main__':unittest.main()
