import contextlib,importlib.util,io,json,os,shutil,subprocess,tempfile,unittest
from pathlib import Path
import numpy as np
from src.hand_ai_flex3 import wide_training as w
from src.hand_ai_flex3.wide_ble import transport_for,contract_for
from src.hand_ai_flex3.wide_record_gui import WideWorker
from test_hand_tonight import fixture

def make2000(raw):
    fixture(raw)
    for p in raw.rglob('*.json'):
        m=json.loads(p.read_text());m['recording_contract']=contract_for(2000)
        if p.name!='session.json':m['acquisition_transport']=transport_for(2000)
        p.write_text(json.dumps(m))

def environment_flags(hand):
    """Expand the actual project's explicit build_flags interpolation for host regression."""
    import configparser,re,shlex
    cfg=configparser.ConfigParser(interpolation=None)
    cfg.read(Path(__file__).resolve().parents[1]/'firmware/esp32/platformio.ini')
    def expand(section):
        value=cfg[section]['build_flags']
        def replacement(match):
            target,option=match.group(1).rsplit('.',1)
            if option!='build_flags':raise AssertionError('Unexpected interpolation')
            return expand(target)
        return re.sub(r'\$\{([^}]+)\}',replacement,value)
    return shlex.split(expand('env:firebeetle32-'+hand+'-glove-flex3-wide2000'))

class BuildEnvironmentTests(unittest.TestCase):
    def test_both_environments_have_exactly_one_2000_definition_and_no_undef(self):
        for hand,device in [('left',1),('right',2)]:
            flags=environment_flags(hand)
            self.assertEqual([x for x in flags if x.startswith('-DGLOVE_GYRO_RANGE_DPS=')],['-DGLOVE_GYRO_RANGE_DPS=2000'])
            self.assertFalse(any(x.startswith('-UGLOVE_GYRO_RANGE_DPS') for x in flags))
            self.assertIn('-DCOMMS_WIDE_GYRO=1',flags)
            self.assertIn('-DCOMMS_FLEX3_GLOVE=1',flags)
            self.assertIn('-DCOMMS_DEVICE_ID='+str(device),flags)
            self.assertIn('-DCOMMS_GLOVE_SEND_ALL=1',flags)

class RangeTests(unittest.TestCase):
    def test_profile_and_environment_selection(self):
        import flash
        self.assertEqual(transport_for(2000)['imu_sensitivity_lsb_per_dps'],16.4)
        self.assertEqual(transport_for(2000)['gyro_scale'],10)
        self.assertTrue(all(x.endswith('wide2000') for x in flash.environments(True,False,True,2000)))
        self.assertTrue(all(x.endswith('wide') for x in flash.environments(True,False,True)))
        with self.assertRaises(ValueError):flash.environments(False,False,True,2000)
        with self.assertRaises(ValueError):transport_for(1500)
    def test_preparation_and_mixed_profile_refusal(self):
        from tools.gyro_trial_audit import audit_trial
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);raw=root/'raw';make2000(raw);w.prepare(raw,root/'data');w.load_data(root/'data/right')
            self.assertEqual(json.loads((root/'data/right/config.json').read_text()),w.config_for(2000))
            p=next((raw/'right').rglob('*.csv'));m=json.loads(p.with_suffix('.json').read_text())
            audit_trial(p,2,m['session'],sensor_version=5,gyro_dps=2000)
            with self.assertRaises(ValueError):audit_trial(p,2,m['session'],sensor_version=5)
            m['acquisition_transport']=transport_for(1000);p.with_suffix('.json').write_text(json.dumps(m))
            with self.assertRaises(ValueError):w.prepare(raw,root/'mixed')
            self.assertFalse((root/'mixed').exists())
    def test_2000_runtime_and_wrong_scale_rejection(self):
        from tools.hand_hls_prepare import prepare
        from src.hand_runtime import ModelBundle
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)/'bundle'
            with contextlib.redirect_stdout(io.StringIO()):prepare(folder,synthetic=True)
            m=json.loads((folder/'manifest.json').read_text());m['scope']='two_trained_hands_validation_export'
            for hand in ('left','right'):
                p=folder/hand/'config.json';p.write_text(json.dumps(w.config_for(2000)));m['generated_sha256'][hand+'/config.json']=w.sha(p)
            (folder/'manifest.json').write_text(json.dumps(m));self.assertEqual(ModelBundle(folder).encode(1,np.zeros((50,12))).shape,(600,))
            p=folder/'right/config.json';cfg=json.loads(p.read_text());cfg['gyro_transport_scale']=100;p.write_text(json.dumps(cfg));m['generated_sha256']['right/config.json']=w.sha(p);(folder/'manifest.json').write_text(json.dumps(m))
            with self.assertRaises(ValueError):ModelBundle(folder)
    def test_guard_preserves_rejected_trial_and_1700_passes(self):
        from common.sensor_v5 import SensorPacket
        def rows(v):return {d:[(i*.02,SensorPacket(d,1,i,i*20,(0,0,0,int(v*10),0,0,0,0,0,0,0,0)),(0,0,0,v,0,0,0,0,0,0,0,0)) for i in range(101)] for d in (1,2)}
        old=os.getcwd()
        with tempfile.TemporaryDirectory() as t:
            os.chdir(t)
            try:
                WideWorker(gyro_dps=2000).validate_rows(rows(1700))
                with self.assertRaisesRegex(ValueError,'1800'):WideWorker(gyro_dps=2000).validate_rows(rows(1800))
                folder=next(Path('.comms-local/rejected_gyro').iterdir());self.assertTrue((folder/'hand_1.csv').exists());self.assertTrue((folder/'hand_2.csv').exists())
            finally:os.chdir(old)
    @unittest.skipUnless(importlib.util.find_spec('torch'),'PyTorch unavailable: run on Windows')
    def test_all12_2000_training_and_export_smoke(self):
        from tools.hand_hls_prepare import prepare
        from src.hand_runtime import ModelBundle
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);make2000(root/'raw');w.prepare(root/'raw',root/'data');w.train(root/'data',root/'models',epochs=1,seeds=(42,))
            for hand in ('left','right'):
                pr=w.Predictor(root/'models/selected'/hand);self.assertFalse(pr.training['right_index_excluded']);self.assertGreater(int(pr.torch.count_nonzero(pr.model.state_dict()['features.0.weight'][:,9,:])),0)
            prepare(root/'bundle',left_run=root/'models/selected/left',right_run=root/'models/selected/right',left_data=root/'data/left',right_data=root/'data/right');ModelBundle(root/'bundle')

@unittest.skipUnless(shutil.which('g++'),'Host C++ compiler unavailable')
class FirmwareTests(unittest.TestCase):
    def test_actual_2000_sensor_readback_conversion_and_failure_from_environments(self):
        from test_gyro_trial_audit import ARDUINO,WIRE
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as t:
            tmp=Path(t);src=tmp/'firmware/esp32/lib/glove_sensor/src';src.mkdir(parents=True)
            shutil.copyfile(root/'hardware/gloves/integration_files/glove_sensor.cpp',src/'glove_sensor.cpp');(src/'glove_sensor.h').write_text('#pragma once\n#define GLOVE_SENSOR_CHANNEL_COUNT 12\n');(tmp/'firmware/esp32/movement_model_params.h').write_text('#define MOVEMENT_MODEL_TRAINED 0\n');(tmp/'Arduino.h').write_text(ARDUINO);(tmp/'Wire.h').write_text(WIRE.replace('badReadback?0x18','badReadback?0x00'))
            p=tmp/'probe.cpp';p.write_text('#include <cassert>\n#include "firmware/esp32/lib/glove_sensor/src/glove_sensor.cpp"\nuint32_t fakeNow=1;\nint main(){assert(gloveBegin());assert(Wire.regs[0x1B]==0x18);Wire.gyro=1740;fakeNow+=10000;gloveUpdate();float out[12];assert(gloveRead(out));assert(std::fabs(out[3]-100)<.0001);Wire.badReadback=true;assert(!gloveBegin());}\n')
            for hand in ('left','right'):
                exe=tmp/('probe_'+hand)
                c=subprocess.run(['g++','-std=c++17','-I',str(tmp),*environment_flags(hand),str(p),'-o',str(exe)],capture_output=True)
                self.assertEqual(c.returncode,0,c.stderr.decode());subprocess.run([str(exe)],check=True)

if __name__=='__main__':unittest.main()
