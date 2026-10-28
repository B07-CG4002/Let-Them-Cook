import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools.gyro_trial_audit import HEADER, audit


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.session='P01_gyro500_check'
        self.folder=self.root/'right'/self.session;self.folder.mkdir(parents=True)
        self.identity=dict(person='P01',session=self.session,split='train',device_id=2,
                           recording_contract='independent_hand_flex3_v1')
        (self.folder/'session.json').write_text(json.dumps(self.identity))
        self.make()

    def make(self, peak=280., name='trial', gap=False):
        path=self.folder/(name+'.csv')
        with path.open('w',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(HEADER)
            for i in range(101):
                # Exercise both uint32 wraps while retaining continuity.
                writer.writerow([i*.02,2,123,(2**32-40+i+(1 if gap and i>=50 else 0))%2**32,
                    (2**32-1000+i*20)%2**32,0,0,0,peak if i==50 else .01,0,0,0,0,0,1,.5,.5])
        meta=dict(self.identity,gesture='WASH',trial=1,csv_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        path.with_suffix('.json').write_text(json.dumps(meta))
        return path

    def run_audit(self,**kwargs):return audit(self.root,self.session,hands=('RIGHT',),minimum=1,**kwargs)

    def test_clean_single_hand_and_uint32_wrap(self):
        r=self.run_audit();self.assertEqual(r['status'],'headroom_observed')
        self.assertEqual(r['counts'],{'RIGHT':{'WASH':1}})
        self.assertEqual(r['trials'][0]['gyro']['gx']['peak_abs'],280)
        self.assertIn('operator declared',r['firmware_range_verification'])

    def test_exact_positive_and_negative_wire_rails_block(self):
        for value in (327.67,-327.68):
            self.make(value)
            r=self.run_audit();self.assertEqual(r['status'],'blocked');self.assertEqual(r['gyro_wire_rail_samples'],1)

    def test_near_rail_requires_review_without_claiming_clipping(self):
        self.make(305)
        r=self.run_audit();self.assertEqual(r['status'],'review_headroom');self.assertEqual(r['gyro_wire_rail_samples'],0)

    def test_changed_csv_hash_blocks(self):
        with (self.folder/'trial.csv').open('a') as stream:stream.write('\n')
        self.assertEqual(self.run_audit()['status'],'blocked')

    def test_sequence_gap_blocks_even_when_rehashed(self):
        self.make(gap=True)
        self.assertEqual(self.run_audit()['status'],'blocked')

    def test_missing_requested_hand_or_gesture_blocks(self):
        self.assertEqual(audit(self.root,self.session,minimum=1)['status'],'blocked')
        self.assertEqual(self.run_audit(gestures=('WASH','THROW'))['status'],'blocked')

    def test_orphan_sidecar_and_wrong_identity_block(self):
        orphan=self.folder/'orphan.json';orphan.write_text('{}')
        self.assertEqual(self.run_audit()['status'],'blocked');orphan.unlink()
        marker=self.folder/'session.json';marker.write_text(json.dumps(dict(self.identity,device_id=1)))
        self.assertEqual(self.run_audit()['status'],'blocked')

    def test_nonfinite_and_unrepresentable_values_block(self):
        for peak in (float('nan'),400,.001):
            self.make(peak)
            self.assertEqual(self.run_audit()['status'],'blocked')

    def test_source_bytes_are_never_modified(self):
        before={p.name:p.read_bytes() for p in self.folder.iterdir()}
        self.run_audit()
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.folder.iterdir()})


ARDUINO=r'''
#pragma once
#include <cstdint>
#include <cstddef>
#include <cmath>
constexpr int INPUT=0;
constexpr double PI=3.141592653589793;
extern uint32_t fakeNow;
inline uint32_t micros(){return fakeNow;}
inline void delay(uint32_t ms){fakeNow+=ms*1000;}
inline void pinMode(int,int){}
inline void analogReadResolution(int){}
inline int analogRead(int){return 2048;}
struct SerialStub {
 void println(const char*){}
 template<class... T> void printf(const char*,T...){}
};
inline SerialStub Serial;
'''
WIRE=r'''
#pragma once
#include <array>
#include <vector>
struct WireStub {
 std::array<uint8_t,256> regs{};
 std::vector<uint8_t> tx,rx;
 size_t cursor=0;uint8_t selected=0;int16_t gyro=100;bool badReadback=false;
 bool begin(int,int){regs[0x75]=0x70;return true;}
 void setClock(int){} void setTimeOut(int){}
 void beginTransmission(uint8_t){tx.clear();}
 void write(uint8_t v){tx.push_back(v);}
 int endTransmission(bool=true){selected=tx[0];if(tx.size()==2)regs[selected]=tx[1];return 0;}
 size_t requestFrom(uint8_t,size_t n){
  rx.clear();cursor=0;
  if(n==1){rx.push_back(selected==0x1B&&badReadback?0x18:regs[selected]);}
  else {int16_t raw[]={0,0,16384,0,gyro,gyro,gyro};
   for(auto v:raw){rx.push_back(static_cast<uint16_t>(v)>>8);rx.push_back(v&255);}}
  return rx.size();
 }
 int available(){return static_cast<int>(rx.size()-cursor);}
 int read(){return rx.at(cursor++);}
};
inline WireStub Wire;
'''


class FirmwareTests(unittest.TestCase):
    def test_module_copies_identical_and_legacy_default_preserved(self):
        root=Path(__file__).resolve().parents[1]
        source=(root/'firmware/esp32/lib/glove_sensor/src/glove_sensor.cpp').read_bytes()
        self.assertEqual(source,(root/'hardware/gloves/integration_files/glove_sensor.cpp').read_bytes())
        self.assertIn(b'#define GLOVE_GYRO_RANGE_DPS 250',source)
        self.assertIn(b'!readRegister(GYRO_CONFIG, gyroConfigReadback)',source)
        config=(root/'firmware/esp32/platformio.ini').read_text()
        self.assertEqual(config.count('-DGLOVE_GYRO_RANGE_DPS=500'),2)

    @unittest.skipUnless(shutil.which('g++'),'Host C++ compiler unavailable; PlatformIO builds are required separately')
    def test_actual_firmware_conversion_bias_and_register_readback(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp);source=work/'firmware/esp32/lib/glove_sensor/src';source.mkdir(parents=True)
            shutil.copyfile(root/'firmware/esp32/lib/glove_sensor/src/glove_sensor.cpp',source/'glove_sensor.cpp')
            (source/'glove_sensor.h').write_text('#pragma once\n#define GLOVE_SENSOR_CHANNEL_COUNT 12\n')
            (work/'firmware/esp32/movement_model_params.h').write_text('#define MOVEMENT_MODEL_TRAINED 0\n')
            (work/'Arduino.h').write_text(ARDUINO);(work/'Wire.h').write_text(WIRE)
            main=work/'main.cpp'
            main.write_text(r'''
#include <cassert>
#include "firmware/esp32/lib/glove_sensor/src/glove_sensor.cpp"
uint32_t fakeNow=1;
int main(){
 assert(gloveBegin());
 assert(Wire.regs[0x1B]==GYRO_CONFIG_VALUE);
 Wire.gyro=6650;fakeNow+=10000;gloveUpdate();float values[12];assert(gloveRead(values));
 const float expected=GLOVE_GYRO_RANGE_DPS==500?100.0f:50.0f;
 assert(std::fabs(values[3]-expected)<0.0001f);
 Wire.badReadback=true;assert(!gloveBegin());
}
''')
            for define in (None,500):
                binary=work/('probe'+str(define))
                command=['g++','-std=c++17','-I',str(work),'-DCOMMS_FLEX3_GLOVE=1']
                if define:command+=['-DGLOVE_GYRO_RANGE_DPS='+str(define)]
                result=subprocess.run(command+[str(main),'-o',str(binary)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                result=subprocess.run([str(binary)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
            bad=subprocess.run(['g++','-std=c++17','-I',str(work),'-DCOMMS_FLEX3_GLOVE=1',
                '-DGLOVE_GYRO_RANGE_DPS=1000',str(main),'-o',str(work/'bad')],capture_output=True,text=True)
            self.assertNotEqual(bad.returncode,0)
            self.assertIn('Only reviewed 250/500',bad.stderr)


if __name__=='__main__':unittest.main()
