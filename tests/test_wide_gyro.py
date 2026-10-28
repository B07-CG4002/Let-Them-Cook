import asyncio
import csv
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from common import sensor_v4, sensor_v5
from common.control import RESPONSE_UUID
from src.hand_ai_flex3 import wide_ble, record_both_gui as dual
from src.hand_ai_flex3.wide_record_gui import WideWorker
from tools.gyro_trial_audit import audit


def stats(device=1,magic=b'LCS5'):
    return struct.pack('<4sBB2xIIIIIII',magic,device,2,42,0,0,0,0,0,0)


class CodecTests(unittest.TestCase):
    def test_physical_units_and_cross_version_refusal(self):
        p=sensor_v5.SensorPacket(1,42,1,20,(1234,0,0,4501,-8002,120,17900,0,0,10000,5000,4000))
        raw=sensor_v5.encode_packet(p);self.assertEqual(len(raw),40)
        self.assertEqual(sensor_v5.physical_values(p.values)[3:6],(450.1,-800.2,12.0))
        self.assertEqual(sensor_v5.physical_values(p.values)[6],179)
        self.assertEqual(sensor_v5.physical_values(p.values)[9:],(1.,.5,.4))
        with self.assertRaises(ValueError):sensor_v4.decode_packet(raw)
        old=sensor_v4.encode_packet(sensor_v4.SensorPacket(1,42,1,20,(0,)*12))
        with self.assertRaises(ValueError):sensor_v5.decode_packet(old)
        with self.assertRaises(ValueError):sensor_v5.split_notification(raw+old)

    def test_stats_reject_v4_wrong_hand_and_bad_flags(self):
        self.assertEqual(wide_ble.parse_stats(stats(),1).boot_id,42)
        for raw in (stats(magic=b'LCS4'),stats(device=2),stats()[:-1]):
            with self.assertRaises(ValueError):wide_ble.parse_stats(raw,1)
        for index,value in ((5,1),(5,8),(6,1)):
            raw=bytearray(stats());raw[index]=value
            with self.assertRaises(ValueError):wide_ble.parse_stats(raw,1)

    def test_queue_decodes_batches_and_rejects_boot_change(self):
        health=dict(dropped=0,malformed=0)
        q=wide_ble.WideQueue(1,42,health)
        p=sensor_v5.SensorPacket(1,42,1,20,(0,0,0,4500,0,0,0,0,0,0,0,0))
        raw=sensor_v5.encode_packet(p);q.feed(raw*2,5.)
        self.assertEqual(q.qsize(),2);self.assertEqual(q.get_nowait()[1][3],450)
        changed=sensor_v5.SensorPacket(1,43,1,20,p.values)
        q.feed(sensor_v5.encode_packet(changed),6.)
        self.assertEqual(health['malformed'],1)

    def test_capture_refuses_near_sensor_range_and_wire_clipping_before_save(self):
        worker=WideWorker((1,))
        for raw in ((0,0,0,9010,0,0,0,0,0,0,0,0),(32767,0,0,100,0,0,0,0,0,0,0,0)):
            p=sensor_v5.SensorPacket(1,42,1,20,raw)
            with self.assertRaises(ValueError):worker.validate_rows({1:[(0.,p,sensor_v5.physical_values(raw))]})


class FakeClient:
    instances=[];magic=b'LCS5'
    def __init__(self,address,**kwargs):
        self.address=address;self.is_connected=False;self.mtu_size=247;self.notifies={};self.writes=[]
        self.__class__.instances.append(self)
    async def connect(self):self.is_connected=True
    async def disconnect(self):self.is_connected=False
    async def read_gatt_char(self,*args,**kwargs):return stats(magic=self.magic)
    async def start_notify(self,uuid,callback):self.notifies[uuid]=callback
    async def write_gatt_char(self,uuid,data,**kwargs):
        self.writes.append(bytes(data));response=bytearray(data);response[3]|=0x80
        self.notifies[RESPONSE_UUID](None,response)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_rate_control_and_matching_v5_notifications(self):
        FakeClient.instances=[];FakeClient.magic=b'LCS5'
        with patch.dict(sys.modules,{'bleak':SimpleNamespace(BleakClient=FakeClient)}):
            async with wide_ble.connection(1) as (client,queue,health):
                self.assertEqual(client.writes[0][3],2)
                self.assertEqual(client.writes[0][14:],b'2\0')
                p=sensor_v5.SensorPacket(1,42,1,20,(0,0,0,4000,0,0,0,0,0,0,0,0))
                client.notifies[sensor_v5.SENSOR_CHARACTERISTIC_UUID](None,sensor_v5.encode_packet(p))
                packet,values,_=await asyncio.wait_for(queue.get(),1)
                self.assertEqual(packet.version,5);self.assertEqual(values[3],400)
                self.assertFalse(any(health.values()))
            self.assertFalse(client.is_connected)

    async def test_wrong_version_is_closed_before_sensor_subscription(self):
        FakeClient.instances=[];FakeClient.magic=b'LCS4'
        with patch.dict(sys.modules,{'bleak':SimpleNamespace(BleakClient=FakeClient)}):
            with self.assertRaisesRegex(ValueError,'requires LCS5'):
                async with wide_ble.connection(1):pass
        self.assertFalse(FakeClient.instances[0].is_connected)
        self.assertEqual(FakeClient.instances[0].notifies,{})
        FakeClient.magic=b'LCS5'

    async def test_wide_capture_marks_contract_provenance_and_audits_450_dps(self):
        with tempfile.TemporaryDirectory() as temp:
            config={1:dual.pair_settings('P01','wide','train','WASH','WASH',1,temp)[1]}
            w=WideWorker((1,));w.links={1:(None,dict(dropped=0,malformed=0))}
            w.trials={1:None}
            async def collect(queue,health):
                return [(i*.02,sensor_v5.SensorPacket(1,42,i,i*20,(0,0,0,4500,0,0,0,0,0,0,0,0)),
                         (0.,0.,0.,450.,0.,0.,0.,0.,0.,0.,0.,0.)) for i in range(101)]
            with patch.object(dual,'collect',collect),patch.object(dual,'WAIT_SECONDS',0):
                await w.batch_both(config)
            folder=Path(temp)/'left/wide'
            meta=json.loads(next(folder.glob('*WASH*.json')).read_text())
            self.assertEqual(meta['recording_contract'],wide_ble.RECORDING_CONTRACT)
            self.assertEqual(meta['acquisition_transport']['gyro_scale'],10)
            self.assertNotIn('pair_capture_id',meta)
            r=audit(temp,'wide',hands=('LEFT',),minimum=1,gyro_dps=1000,sensor_version=5)
            self.assertEqual(r['status'],'headroom_observed',r['errors'])
            self.assertEqual(r['trials'][0]['gyro']['gx']['peak_abs'],450)
            # Legacy audit/training import must not silently accept diagnostic v5.
            self.assertEqual(audit(temp,'wide',hands=('LEFT',),minimum=1)['status'],'blocked')


@unittest.skipUnless(shutil.which('g++'),'Host C++ unavailable; build both PlatformIO targets separately')
class FirmwareTests(unittest.TestCase):
    def test_actual_packet_scaler_and_stats_cross_language_v3_v4_v5(self):
        root=Path(__file__).resolve().parents[1]
        include=root/'firmware/esp32/include'
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp);source=work/'test.cpp'
            source.write_text(r'''
#include <cstdio>
#include "comms_source_stats.h"
int main(){
 float input[comms::kChannelCount]={};input[0]=0.1234f;input[3]=450.1f;
 int16_t words[comms::kChannelCount];comms::SampleScaleStats count={};
 if(!comms::scaleSample(input,words,count))return 2;
 unsigned char packet[40]={},stats[36]={};comms::SourceStats s={42,1,1,0};comms::SampleCounters c={};
 if(!comms::serializePacket(packet,sizeof(packet),1,42,0,20,words))return 3;
 if(!comms::serializeSourceStats(stats,sizeof(stats),1,false,s,c))return 4;
 std::fwrite(packet,1,comms::kPacketSize,stdout);std::fwrite(stats,1,36,stdout);
}
''')
            for version in (3,4,5):
                flags=[]
                if version>=4:flags+=['-DCOMMS_FLEX3_GLOVE=1']
                if version==5:flags+=['-DCOMMS_WIDE_GYRO=1']
                exe=work/('probe'+str(version))
                r=subprocess.run(['g++','-std=c++17','-I',str(include),*flags,str(source),'-o',str(exe)],capture_output=True)
                self.assertEqual(r.returncode,0,r.stderr.decode())
                r=subprocess.run([str(exe)],capture_output=True);self.assertEqual(r.returncode,0)
                size=38 if version==3 else 40
                self.assertEqual(r.stdout[2],version);self.assertEqual(r.stdout[size:size+4],b'LCS'+str(version).encode())
                rawgyro=struct.unpack_from('<h',r.stdout,22)[0]
                self.assertEqual(rawgyro,4501 if version==5 else 32767)
                if version==5:
                    packet=sensor_v5.decode_packet(r.stdout[:40]);self.assertEqual(sensor_v5.physical_values(packet.values)[3],450.1)

    def test_actual_sensor_1000_range_conversion_and_readback(self):
        from test_gyro_trial_audit import ARDUINO,WIRE
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp);src=work/'firmware/esp32/lib/glove_sensor/src';src.mkdir(parents=True)
            shutil.copyfile(root/'firmware/esp32/lib/glove_sensor/src/glove_sensor.cpp',src/'glove_sensor.cpp')
            (src/'glove_sensor.h').write_text('#pragma once\n#define GLOVE_SENSOR_CHANNEL_COUNT 12\n')
            (work/'firmware/esp32/movement_model_params.h').write_text('#define MOVEMENT_MODEL_TRAINED 0\n')
            (work/'Arduino.h').write_text(ARDUINO);(work/'Wire.h').write_text(WIRE)
            source=work/'main.cpp';source.write_text(r'''
#include <cassert>
#include "firmware/esp32/lib/glove_sensor/src/glove_sensor.cpp"
uint32_t fakeNow=1;
int main(){
 assert(gloveBegin());assert(Wire.regs[0x1B]==0x10);
 Wire.gyro=3280+100;fakeNow+=10000;gloveUpdate();float values[12];assert(gloveRead(values));
 assert(std::fabs(values[3]-100)<.0001);
 Wire.badReadback=true;assert(!gloveBegin());
}
''')
            exe=work/'probe'
            r=subprocess.run(['g++','-std=c++17','-I',str(work),'-DCOMMS_FLEX3_GLOVE=1','-DCOMMS_WIDE_GYRO=1',
                '-DGLOVE_GYRO_RANGE_DPS=1000',str(source),'-o',str(exe)],capture_output=True)
            self.assertEqual(r.returncode,0,r.stderr.decode())
            r=subprocess.run([str(exe)],capture_output=True);self.assertEqual(r.returncode,0,r.stderr)


if __name__=='__main__':unittest.main()
