import csv
import asyncio
import contextlib
import io
from contextlib import asynccontextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from src.hand_ai_flex3 import wide_training as w
from src.hand_ai_flex3 import record_gui
from src.hand_ai_flex3.wide_ble import RECORDING_CONTRACT,TRANSPORT
from tools import hand_tonight


def fixture(root):
    for d,hand in ((1,'LEFT'),(2,'RIGHT')):
        for split,count in (('train',8),('validation',1),('test',1)):
            session='P01_'+split
            for label,g in enumerate(w.LABELS):
                config=record_gui.settings('P01',session,split,hand,g,count,str(root))
                config['recording_contract']=RECORDING_CONTRACT
                numbers=record_gui.prepare_session(config)
                for trial in numbers:
                    ordinal=(label*10+trial)+(100 if split=='validation' else 200 if split=='test' else 0)
                    rows=[]
                    for i in range(101):
                        v=np.array([.01*np.sin(i*.07+c+ordinal*.1+d) for c in range(12)])
                        v[3:6]=(label+1)*10+np.sin(i*.1+ordinal)*4
                        v[9:]=.4+.1*np.sin(i*.04+ordinal+np.arange(3));v[0]+=.0001*ordinal
                        scales=np.array([10000]*3+[10]*3+[100]*3+[10000]*3)
                        v=np.round(v*scales)/scales
                        p=SimpleNamespace(device_id=d,boot_id=42+d,seq=ordinal*101+i,uptime_ms=ordinal*2200+i*20,
                                          values=tuple(np.rint(v*scales).astype(int)))
                        rows.append((i*.02,p,v.tolist()))
                    path,*_=record_gui.save_trial(config,trial,rows)
                    meta=json.loads(path.with_suffix('.json').read_text());meta['acquisition_transport']=TRANSPORT
                    path.with_suffix('.json').write_text(json.dumps(meta))


class DataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)/'raw';fixture(cls.root)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_two_hands_prepare_explicit_config_and_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'prepared';r=w.prepare(self.root,out)
            self.assertEqual(r['trials'],{'1':70,'2':70})
            for d,h in ((1,'left'),(2,'right')):
                x,y,rows,m=w.load_data(out/h)
                self.assertEqual(x.shape,(70,50,12));self.assertEqual(m['device_id'],d)
                self.assertEqual(json.loads(rows[0]['acquisition_transport_json']),TRANSPORT)
                self.assertEqual(json.loads((out/h/'config.json').read_text()),w.WIDE_CONFIG)

    def test_ratios_and_session_separation(self):
        result=w.progress(self.root)
        self.assertEqual(result['left']['status'],'ratio_complete')
        self.assertEqual(result['right']['counts']['WASH'],{'train':8,'validation':1,'test':1})
        # Each split has an independent session; changing one manifest identity
        # is refused before a prepared output can be created.
        path=next((self.root/'left/P01_validation').glob('*.csv'));meta=path.with_suffix('.json')
        before=meta.read_bytes()
        try:
            value=json.loads(before);value['session']='P01_train';meta.write_text(json.dumps(value))
            with tempfile.TemporaryDirectory() as temp:
                out=Path(temp)/'prepared'
                with self.assertRaises(ValueError):w.prepare(self.root,out)
                self.assertFalse(out.exists())
        finally:meta.write_bytes(before)

    def test_raw_hash_tamper_blocks_before_output(self):
        path=next(self.root.rglob('*.csv'));before=path.read_bytes()
        try:
            path.write_bytes(before+b'\n')
            with tempfile.TemporaryDirectory() as temp:
                out=Path(temp)/'prepared'
                with self.assertRaises(ValueError):w.prepare(self.root,out)
                self.assertFalse(out.exists())
        finally:path.write_bytes(before)

    def test_dataset_tamper_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'prepared';w.prepare(self.root,out)
            x=np.load(out/'right/X.npy');x[0,0,0]+=1;np.save(out/'right/X.npy',x)
            with self.assertRaisesRegex(ValueError,'hash changed'):w.load_data(out/'right')

    def test_macro_f1_includes_missing_predictions(self):
        result=w.metrics(np.arange(7),np.zeros(7,dtype=int))
        self.assertAlmostEqual(result['accuracy'],1/7)
        self.assertAlmostEqual(result['macro_f1'],(2/8)/7)

    @unittest.skipUnless(importlib.util.find_spec('torch'),'PyTorch required: run on the Windows training environment')
    def test_real_training_right_mask_test_gate_and_fpga_source_loader(self):
        from tools import precision_sweep as q
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);prepared=folder/'prepared';w.prepare(self.root,prepared)
            r=w.train(prepared,folder/'trained',epochs=1,seeds=(42,))
            self.assertFalse(r['test_evaluated']);selected=folder/'trained/selected'
            right=w.Predictor(selected/'right');x,_,records,_=w.load_data(prepared/'right')
            before=right.probabilities(x[:3]);probe=x[:3].copy();probe[:,:,9]=np.array([0,.5,1])[:,None]
            np.testing.assert_allclose(before,right.probabilities(probe),atol=1e-7,rtol=1e-6)
            mean=np.mean(x[[i for i,r in enumerate(records) if r['split']=='train']],axis=(0,1),dtype=np.float64)
            active=[i for i in range(12) if i!=9]
            np.testing.assert_allclose(right.mean[active],mean[active],rtol=1e-5,atol=1e-6)
            self.assertEqual(right.mean[9],0);self.assertEqual(right.std[9],1)
            for hand in ('left','right'):
                source=q.load_source(run=selected/hand)
                val,y,calibration,info=q.load_data(prepared/hand,source,'validation')
                self.assertEqual(val.shape,(7,50,12));self.assertEqual(len(calibration),56)
                self.assertEqual(info['split'],'validation')
            with self.assertRaises(ValueError):w.evaluate(selected,prepared,'test',folder/'test.json')
            report=w.evaluate(selected,prepared,'test',folder/'test.json',allow_test=True)
            self.assertTrue(report['test_use_recorded']);self.assertEqual(report['models']['left']['metrics']['samples'],7)


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_live_sources_cleanup_and_ordered_capture(self):
        from src.hand_ai_flex3 import wide_ble
        from src.hand_ai_lab.data import load_capture
        closed=[];virtual_now=[1000.]
        @asynccontextmanager
        async def connection(d):
            queue=asyncio.Queue();health=dict(dropped=0,malformed=0)
            # Complete two seconds of source data without relying on OS sleep
            # resolution. Callback times and the live clock share this virtual
            # timebase. Real transport pacing is outside this mocked flow test.
            for i in range(101):
                p=SimpleNamespace(device_id=d,boot_id=42+d,seq=i,uptime_ms=i*20)
                queue.put_nowait((p,(0.,)*12,1000.+i*.02))
            virtual_now[0]=1002.
            try:yield None,queue,health
            finally:closed.append(d)
        class FakePredictor:
            def __init__(self,run):self.device_id=1 if Path(run).name=='left' else 2
            def probabilities(self,x):return np.array([[0.,0.,0.,1.,0.,0.,0.]])
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);runs=root/'models'
            for h in ('left','right'):
                (runs/h).mkdir(parents=True);(runs/h/'model.pth').write_bytes(b'local mocked backend fixture')
            duration_token=1234.
            a=SimpleNamespace(hands='BOTH',runs=runs,out=root/'live',threshold=.7,person='P01',session='livecheck',seconds=duration_token)
            class TestAsyncio:
                def __getattr__(self,name):return getattr(asyncio,name)
                async def sleep(self,seconds):
                    if seconds!=duration_token:
                        await asyncio.sleep(0);return
                    # Finish only after the real live loop has emitted one
                    # prediction for each hand; do not replace that loop or its
                    # assertions with a timer assumption. Outer wait_for is a
                    # failure watchdog, not the condition for a passing test.
                    path=root/'live/predictions.jsonl'
                    while True:
                        records=[json.loads(line) for line in path.read_text().splitlines()]
                        predicted={r['device_id'] for r in records if r['type']=='prediction'}
                        if predicted=={1,2}:return
                        await asyncio.sleep(.001)
            with patch.object(wide_ble,'connection',connection),patch.object(w,'Predictor',FakePredictor), \
                 patch.object(hand_tonight,'time',SimpleNamespace(monotonic=lambda:virtual_now[0])), \
                 patch.object(hand_tonight,'asyncio',TestAsyncio()):
                result=await asyncio.wait_for(hand_tonight.live(a),timeout=30)
            self.assertEqual(set(closed),{1,2});self.assertGreater(result['predictions'][1],0);self.assertGreater(result['predictions'][2],0)
            meta,samples=load_capture(root/'live/capture.jsonl')
            self.assertEqual({r['device_id'] for r in samples},{1,2})
            self.assertEqual(meta['sensor_packet_version'],5)
            report=json.loads((root/'live/live_summary.json').read_text());self.assertFalse(report['game_commands_sent'])


class BundleContractTests(unittest.TestCase):
    def test_v5_configuration_loads_and_wrong_scale_is_refused(self):
        from tools.hand_hls_prepare import prepare
        from src.hand_runtime import ModelBundle
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)/'fixture'
            with contextlib.redirect_stdout(io.StringIO()):prepare(folder,synthetic=True)
            # A temporary contract fixture only; no trained accuracy is claimed.
            manifest=json.loads((folder/'manifest.json').read_text())
            manifest['scope']='two_trained_hands_validation_export'
            for hand in ('left','right'):
                path=folder/hand/'config.json';path.write_text(json.dumps(w.WIDE_CONFIG))
                manifest['generated_sha256'][hand+'/config.json']=w.sha(path)
            (folder/'manifest.json').write_text(json.dumps(manifest))
            bundle=ModelBundle(folder);self.assertFalse(bundle.synthetic)
            self.assertEqual(bundle.encode(1,np.zeros((50,12))).shape,(600,))
            bad=dict(w.WIDE_CONFIG,gyro_transport_scale=100)
            path=folder/'right/config.json';path.write_text(json.dumps(bad))
            manifest['generated_sha256']['right/config.json']=w.sha(path)
            (folder/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError,'Wide-gyro preprocessing'):ModelBundle(folder)


if __name__=='__main__':unittest.main()
