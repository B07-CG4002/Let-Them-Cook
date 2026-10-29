"""Cross-language packet and ADC-path checks; no Bluetooth hardware required."""
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
import unittest
import numpy as np
from src.hand_ai_flex3.sensor import SensorPacket, encode_packet, decode_packet, split_notification, physical_values
from src.hand_ai_flex3.source_stats import parse_source_stats
from src.hand_ai_flex3.core import resample

ROOT=Path(__file__).resolve().parents[1]

class ProtocolTests(unittest.TestCase):
    def test_third_flex_survives_six_sample_batch(self):
        packets=[SensorPacket(2,7,i,i*20,tuple(range(11))+(i*1000,)) for i in range(6)]
        raw=b''.join(map(encode_packet,packets))
        self.assertEqual(len(raw),240)
        decoded=[decode_packet(p) for p in split_notification(raw)]
        self.assertEqual(decoded,packets)
        self.assertAlmostEqual(physical_values(decoded[-1].values)[-1],.5)
        with self.assertRaises(ValueError):split_notification(raw+b'X')
        with self.assertRaises(ValueError):decode_packet(encode_packet(packets[0])[:38])

    def test_old_firmware_and_wrong_hand_are_refused(self):
        raw=struct.pack('<4sBB2xIIIIIII',b'LCS4',2,2,7,0,0,0,0,0,0)
        self.assertEqual(parse_source_stats(raw,expected_device_id=2).boot_id,7)
        with self.assertRaises(ValueError):parse_source_stats(raw,expected_device_id=1)
        with self.assertRaisesRegex(ValueError,'LCS4'):parse_source_stats(b'LCS3'+raw[4:])

    def test_third_flex_is_resampled_and_old_shape_refused(self):
        values=np.zeros((101,12));values[:,-1]=np.linspace(0,1,101)
        got=resample(np.arange(101)*.02,values)
        np.testing.assert_allclose(got[:,-1],np.linspace(0,1,50),atol=1e-6)
        with self.assertRaises(ValueError):resample(np.arange(101)*.02,values[:,:11])

    def test_actual_firmware_sensor_and_wire_layout_both_modes(self):
        compiler=shutil.which('g++') or shutil.which('clang++')
        if not compiler:self.skipTest('Native C++ compiler unavailable; run PlatformIO builds separately')
        arduino=r'''
#pragma once
#include <cstdint>
#include <cstddef>
#include <cmath>
#define INPUT 0
#define PI 3.14159265358979323846
extern uint32_t fakeNow;
extern int read39;
inline uint32_t micros(){return fakeNow;}
inline uint32_t millis(){return fakeNow/1000;}
inline void delay(unsigned ms){fakeNow+=ms*1000;}
inline void analogReadResolution(int){}
inline void pinMode(int,int){}
inline int analogRead(int pin){if(pin==39){++read39;return 3000;}return pin==34?1000:2000;}
struct SerialStub { void println(const char*){} template<class... T> void printf(const char*,T...){} }; extern SerialStub Serial;
'''
        wire=r'''
#pragma once
#include <cstddef>
#include <cstdint>
struct WireStub {
 int reg=0,pos=0,count=0;
 bool begin(int,int){return true;} void setClock(int){} void setTimeOut(int){}
 void beginTransmission(int){} void write(int value){reg=value;}
 int endTransmission(bool=true){return 0;}
 size_t requestFrom(uint8_t,size_t n){pos=0;count=int(n);return n;}
 int available(){return count-pos;}
 int read(){int p=pos++;return count==1?(reg==0x75?0x70:0):(p==4?0x40:0);}
}; extern WireStub Wire;
'''
        harness=r'''
#include "Arduino.h"
#include "Wire.h"
#include "glove_sensor.h"
#include "comms_sample.h"
#include "comms_source_stats.h"
#include "comms_send_queue.h"
#include <cassert>
#include <cstdio>
uint32_t fakeNow=0;int read39=0;SerialStub Serial;WireStub Wire;
int main(){
 assert(gloveBegin());fakeNow+=20000;gloveUpdate();
 float sample[comms::kChannelCount]={};assert(gloveRead(sample));
 assert(std::fabs(sample[9]-1000.0f/4095)<1e-6);
 assert(std::fabs(sample[10]-2000.0f/4095)<1e-6);
#if COMMS_FLEX3_GLOVE
 static_assert(comms::kPacketSize==40 && comms::kChannelCount==12,"flex3 layout");
 assert(read39>0);assert(std::fabs(sample[11]-3000.0f/4095)<1e-6);
#else
 static_assert(comms::kPacketSize==38 && comms::kChannelCount==11,"legacy layout");
 assert(read39==0);
#endif
 int16_t scaled[comms::kChannelCount]={};comms::SampleScaleStats scale={};
 assert(comms::scaleSample(sample,scaled,scale));assert(scale.clamped==0);
 uint8_t packet[comms::kPacketSize]={};
 assert(comms::serializePacket(packet,sizeof(packet),2,7,9,2000,scaled));
 for(auto b:packet)std::printf("%02x",b);std::printf("\n");
 uint8_t stats[36]={};comms::SourceStats source={};comms::SampleCounters counters={};
 assert(comms::serializeSourceStats(stats,sizeof(stats),2,false,source,counters));
 assert(stats[3]==(COMMS_FLEX3_GLOVE?'4':'3'));
 assert(comms::packetsPerNotification(517)==6);
 assert(comms::packetsPerNotification(comms::kPacketSize+2)==0);
}
'''
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'Arduino.h').write_text(arduino);(p/'Wire.h').write_text(wire)
            (p/'main.cpp').write_text(harness)
            for mode in (0,1):
                with self.subTest(mode=mode):
                    exe=p/f'firmware-{mode}.exe'
                    cmd=[compiler,'-std=c++11','-Wall','-Wextra',f'-DCOMMS_FLEX3_GLOVE={mode}',
                         '-I',str(p),'-I',str(ROOT/'firmware/esp32/include'),
                         '-I',str(ROOT/'firmware/esp32/lib/glove_sensor/src'),
                         str(p/'main.cpp'),str(ROOT/'firmware/esp32/lib/glove_sensor/src/glove_sensor.cpp'),'-o',str(exe)]
                    run=subprocess.run(cmd,capture_output=True,text=True)
                    self.assertEqual(run.returncode,0,run.stderr)
                    run=subprocess.run([str(exe)],capture_output=True,text=True)
                    self.assertEqual(run.returncode,0,run.stderr)
                    raw=bytes.fromhex(run.stdout.strip())
                    if mode:
                        packet=decode_packet(raw)
                        self.assertEqual(packet.values[-1],round(3000/4095*10000))
                    else:
                        from common.sensor import decode_packet as old_decode
                        self.assertEqual(len(old_decode(raw).values),11)

if __name__=='__main__':unittest.main()
