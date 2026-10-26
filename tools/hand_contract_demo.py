"""Offline v4 framing + scripted prediction/event/consumer integration check."""
import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path

from common.hand_contract import SensorV4,decode_notification,to_bridge_sample,make_hello
from common.wire import encode_frame,read_frame
from src.hand_runtime import ActionFilter
from src.hand_events import EventEncoder,EventConsumer
from ultra96.hand_admission import HandAdmission


async def roundtrip(message):
    reader=asyncio.StreamReader();reader.feed_data(encode_frame(message));reader.feed_eof()
    return await read_frame(reader)


async def run(out):
    out=Path(out)
    if out.exists():raise FileExistsError('Choose a NEW demo output directory')
    hello=make_hello('ltc-hand-contract-demo','1234567890abcdef1234567890abcdef')
    encoder=EventEncoder(hello);consumer=EventConsumer(hello);gate=HandAdmission(hello)
    filters={1:ActionFilter(),2:ActionFilter()}
    sensors=[];samples=[];events=[];outcomes=[];dropped=0;duplicate=None
    for seq in range(221):
        now=seq/50.
        for device in (1,2):
            packet=SensorV4(device,100+device,seq,seq*20,
                (100,-200,10000,100,-200,300,1000,-2000,3000,4000,5000,6000))
            decoded=decode_notification(packet.encode())[0]
            frame=await roundtrip(decoded.message(hello['session_id']))
            gate.accepted_sample(frame,now);sensors.append(frame);samples.append(to_bridge_sample(frame))
            records=[]
            if 100<=seq<=170 and seq%10==0:
                gesture=('CHOP' if seq<=140 else 'POURING') if device==1 else 'PICKUP'
                base=dict(device_id=device,elapsed_s=now,boot_id=packet.boot_id,seq=seq,uptime_ms=seq*20)
                records.append(dict(base,type='prediction',gesture=gesture,confidence=.95))
                records.extend(dict(base,**event) for event in filters[device].update(gesture,.95,now))
            else:
                records.extend(dict(event,device_id=device,elapsed_s=now) for event in filters[device].tick(now))
            for record in records:
                for event in encoder.consume(record):
                    event=await roundtrip(event);gate.event(event,now);events.append(event)
                    if event['kind']=='state_end' and event['gesture']=='POURING' and event['reason']=='prediction_timeout':
                        dropped+=1;continue  # deliberate fault: prove consumer lease protection
                    outcome=consumer.apply(event,now);outcomes.append(outcome)
                    if event['kind']=='pulse':duplicate=event
    if duplicate is not None:outcomes.append(consumer.apply(await roundtrip(duplicate),4.4))
    consumer.expire(5.)
    transition=[e for e in events if e['device_id']==1 and e['source'] and e['source']['seq']==170
                and ((e['kind']=='state_end' and e['gesture']=='CHOP') or
                     (e['kind']=='state_start' and e['gesture']=='POURING'))]
    summary=dict(scope='offline framing and scripted-score contract simulation; no BLE/TLS/FPGA/phone',
        sensor_frames=len(sensors),bridge_samples=len(samples),event_frames=len(events),
        accepted_pulses=len(consumer.pulses),outcomes=dict(Counter(outcomes)),
        same_source_transition_events=len(transition),same_source_distinct_event_ids=len({e['event_id'] for e in transition}),
        deliberately_dropped_state_ends=dropped,lease_expirations=sum(e['reason']=='lease_expired' for e in consumer.ends),
        active_states_after_expiry=consumer.active,gameplay_executed=False,production_service_changed=False)
    assert len(sensors)==442 and len(samples)==442
    assert len(consumer.pulses)==1 and outcomes.count('duplicate')==1
    assert len(transition)==2 and len({e['event_id'] for e in transition})==2
    assert dropped==1 and summary['lease_expirations']==1 and all(v is None for v in consumer.active.values())
    out.mkdir(parents=True)
    (out/'hello.json').write_text(json.dumps(hello,indent=2)+'\n',encoding='utf-8')
    for name,rows in (('sensor_frames.jsonl',sensors),('bridge_samples.jsonl',samples),('hand_events.jsonl',events)):
        with (out/name).open('w',encoding='utf-8',newline='\n') as stream:
            for row in rows:stream.write(json.dumps(row,allow_nan=False)+'\n')
    (out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,indent=2));return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',required=True)
    args=parser.parse_args();asyncio.run(run(args.out))


if __name__=='__main__':main()
