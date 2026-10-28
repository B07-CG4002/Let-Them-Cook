import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
from src.hand_ai_flex3 import record_both_gui as dual


def rows(d):
    return [(i*.02,SimpleNamespace(device_id=d,boot_id=10+d,seq=i,uptime_ms=i*20,values=(0,)*12),
             np.sin(np.arange(12)+i*.02+d).tolist()) for i in range(101)]


class PairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config=dual.pair_settings('P01','dual1','train','CHOP','THROW',1,self.temp.name)
        for d in (1,2):dual.prepare_session(self.config[d])
    def tearDown(self):self.temp.cleanup()
    def test_separate_native_files_retain_all_channels_and_pair_provenance(self):
        pair=dual.save_pair(self.config,{1:1,2:1},{d:rows(d) for d in (1,2)})
        metadata=[]
        for d in (1,2):
            path=next(self.config[d]['root'].glob('*.csv'))
            self.assertIn('flex3',path.read_text().splitlines()[0])
            m=json.loads(path.with_suffix('.json').read_text());metadata.append(m)
            self.assertEqual((m['device_id'],m['pair_capture_id']),(d,pair))
        self.assertEqual([m['gesture'] for m in metadata],['CHOP','THROW'])
    def test_existing_trial_is_never_overwritten(self):
        dual.save_pair(self.config,{1:1,2:1},{d:rows(d) for d in (1,2)})
        before={str(p):p.read_bytes() for p in Path(self.temp.name).rglob('*') if p.is_file()}
        with self.assertRaises(FileExistsError):dual.save_pair(self.config,{1:1,2:1},{d:rows(d) for d in (1,2)})
        self.assertEqual(before,{str(p):p.read_bytes() for p in Path(self.temp.name).rglob('*') if p.is_file()})
    def test_invalid_other_hand_does_not_save_first_hand(self):
        with self.assertRaises(ValueError):dual.save_pair(self.config,{1:1,2:1},{1:rows(1),2:rows(2)[:2]})
        self.assertEqual(list(Path(self.temp.name).rglob('*.csv')),[])
    def test_partial_commit_failure_rolls_back_only_new_files(self):
        original=dual.os.link;count=0
        def fail(source,target):
            nonlocal count
            count+=1
            if count==3:raise OSError('simulated disk failure')
            original(source,target)
        with patch.object(dual.os,'link',side_effect=fail):
            with self.assertRaises(OSError):dual.save_pair(self.config,{1:1,2:1},{d:rows(d) for d in (1,2)})
        self.assertEqual(list(Path(self.temp.name).rglob('*.csv')),[])
    def test_resume_numbers_independent(self):
        dual.save_pair(self.config,{1:7,2:12},{d:rows(d) for d in (1,2)})
        self.assertEqual(dual.prepare_session(self.config[1]),[8])
        self.assertEqual(dual.prepare_session(self.config[2]),[13])
    def test_session_split_cannot_change(self):
        bad=dict(self.config[1],split='test')
        with self.assertRaises(ValueError):dual.prepare_session(bad)
    def test_single_hand_save_has_no_fake_pair_or_other_hand_file(self):
        for d in (1,2):
            with tempfile.TemporaryDirectory() as temp:
                config=dual.pair_settings('P01','solo1','train','IDLE','IDLE',1,temp)
                config={d:config[d]};dual.prepare_session(config[d])
                capture=dual.save_pair(config,{d:1},{d:rows(d)})
                paths=list(Path(temp).rglob('*.csv'));self.assertEqual(len(paths),1)
                meta=json.loads(paths[0].with_suffix('.json').read_text())
                self.assertEqual(meta['captured_devices'],[d]);self.assertEqual(meta['capture_id'],capture)
                self.assertNotIn('pair_capture_id',meta);self.assertNotIn('other_device_id',meta)
                self.assertFalse((Path(temp)/('right' if d==1 else 'left')).exists())
    def test_wrong_hand_and_source_gap_refused(self):
        for bad in (rows(1),rows(2)[:30]+rows(2)[31:]):
            with self.assertRaises(ValueError):dual.save_pair(self.config,{1:1,2:1},{1:rows(1),2:bad})
        self.assertEqual(list(Path(self.temp.name).rglob('*.csv')),[])


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_modes_never_connect_unselected_glove(self):
        for devices in ((1,),(2,)):
            opened=[];closed=[]
            @asynccontextmanager
            async def connection(d):
                opened.append(d)
                try:yield None,asyncio.Queue(),dict(dropped=0,malformed=0)
                finally:closed.append(d)
            async def collect(queue,health):return rows(queue.device)
            with tempfile.TemporaryDirectory() as temp:
                w=dual.BothWorker(devices)
                full=dual.pair_settings('P01','solo','train','IDLE','IDLE',1,temp)
                with patch.object(dual,'connection',connection),patch.object(dual,'collect',collect),patch.object(dual,'WAIT_SECONDS',0):
                    task=asyncio.create_task(w.supervise());w.commands.put(dict(settings={d:full[d] for d in devices}))
                    for _ in range(100):
                        if any(e['kind']=='complete' for e in w.events):break
                        await asyncio.sleep(.01)
                    self.assertTrue(any(e['kind']=='complete' for e in w.events))
                    self.assertEqual(opened,list(devices));self.assertEqual(set(w.latest),set())
                    w.stop.set();await asyncio.wait_for(task,2)
                self.assertEqual(closed,list(devices));self.assertEqual(len(list(Path(temp).rglob('*.csv'))),1)
    async def test_second_connection_failure_closes_first(self):
        closed=[]
        @asynccontextmanager
        async def connection(d):
            if d==2:raise ConnectionError('RIGHT unavailable')
            try:yield None,asyncio.Queue(),dict(dropped=0,malformed=0)
            finally:closed.append(d)
        w=dual.BothWorker()
        with patch.object(dual,'connection',connection):
            with self.assertRaises(ConnectionError):await w.supervise()
        self.assertEqual(closed,[1]);self.assertFalse(any(e['kind']=='connected' for e in w.events))

    async def test_two_connections_batch_and_cleanup(self):
        closed=[]
        @asynccontextmanager
        async def connection(d):
            try:yield None,asyncio.Queue(),dict(dropped=0,malformed=0)
            finally:closed.append(d)
        async def collect(queue,health):return rows(queue.device)
        with tempfile.TemporaryDirectory() as temp:
            worker=dual.BothWorker();config=dual.pair_settings('P01','run1','train','WASH','WASH',1,temp)
            with patch.object(dual,'connection',connection),patch.object(dual,'collect',collect),patch.object(dual,'WAIT_SECONDS',0):
                task=asyncio.create_task(worker.supervise())
                for _ in range(100):
                    if any(e['kind']=='connected' for e in worker.events):break
                    await asyncio.sleep(.005)
                worker.commands.put(dict(settings=config))
                for _ in range(100):
                    if any(e['kind']=='complete' for e in worker.events):break
                    await asyncio.sleep(.01)
                self.assertTrue(any(e['kind']=='complete' for e in worker.events))
                worker.stop.set();await asyncio.wait_for(task,2)
            self.assertEqual(set(closed),{1,2})
            self.assertEqual(len(list(Path(temp).rglob('*.csv'))),2)
    async def test_cancel_during_capture_saves_neither_and_keeps_links(self):
        closed=[];started=asyncio.Event()
        @asynccontextmanager
        async def connection(d):
            try:yield None,asyncio.Queue(),dict(dropped=0,malformed=0)
            finally:closed.append(d)
        async def collect(queue,health):started.set();await asyncio.Future()
        with tempfile.TemporaryDirectory() as temp:
            w=dual.BothWorker();config=dual.pair_settings('P01','cancel1','train','IDLE','IDLE',1,temp)
            with patch.object(dual,'connection',connection),patch.object(dual,'collect',collect),patch.object(dual,'WAIT_SECONDS',0):
                task=asyncio.create_task(w.supervise());w.commands.put(dict(settings=config))
                await asyncio.wait_for(started.wait(),1);w.cancel_recording.set()
                for _ in range(100):
                    if any(e['kind']=='cancelled' for e in w.events):break
                    await asyncio.sleep(.01)
                self.assertTrue(any(e['kind']=='cancelled' for e in w.events))
                self.assertEqual(closed,[]);self.assertFalse(task.done())
                self.assertEqual(list(Path(temp).rglob('*.csv')),[])
                w.stop.set();await asyncio.wait_for(task,2)


class MaskTests(unittest.TestCase):
    def test_folded_first_convolution_matches_mask_and_ignores_index(self):
        from tools.hand_index_fallback import fold_index,INDICES
        class Array(np.ndarray):
            def clone(self):return self.copy()
        rng=np.random.default_rng(4)
        weights=rng.normal(size=(32,12,3)).astype(np.float32).view(Array)
        state={'features.0.weight':weights}
        folded=fold_index(state)['features.0.weight']
        self.assertTrue(np.any(weights[:,9,:]!=0));self.assertTrue(np.all(folded[:,9,:]==0))
        x=rng.normal(size=(12,50)).astype(np.float32);masked=x.copy();masked[9]=0
        def conv(a,w):return np.einsum('ctk,ock->ot',np.lib.stride_tricks.sliding_window_view(a,3,axis=1),w)
        np.testing.assert_allclose(conv(x,folded),conv(masked,weights),atol=1e-5)
        x[9]=rng.normal(size=50)*100
        np.testing.assert_allclose(conv(x,folded),conv(masked,weights),atol=1e-5)
        self.assertEqual(INDICES,[0,1,2,3,4,5,6,7,8,10,11])
    def test_wrong_architecture_refused(self):
        from tools.hand_index_fallback import fold_index
        class Array(np.ndarray):
            def clone(self):return self.copy()
        with self.assertRaises(ValueError):fold_index({'features.0.weight':np.zeros((32,11,3)).view(Array)})

    @unittest.skipUnless(importlib.util.find_spec('torch'),'PyTorch required: run this test on the Windows training environment')
    def test_real_training_export_and_native_prediction_ignore_index(self):
        import csv,hashlib
        from tools import hand_index_fallback as fallback
        from src.hand_ai_flex3.core import CONFIG,LABELS
        from src.hand_ai_flex3.pipeline import Predictor
        from src.hand_ai_lab import training as lab
        rng=np.random.default_rng(9);x=[];y=[];metadata=[]
        for label,g in enumerate(LABELS):
            for split,count in [('train',8),('validation',1),('test',1)]:
                for n in range(count):
                    v=rng.normal(size=(50,12)).astype(np.float32);x.append(v);y.append(label)
                    metadata.append(dict(person='P01',session=split,gesture=g,trial=n+1,split=split,
                        device_id=2,source='synthetic_fixture',sha256='fixture',input_sha256=hashlib.sha256(v.tobytes()).hexdigest()))
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);data=root/'data';data.mkdir()
            np.save(data/'X.npy',np.array(x));np.save(data/'y.npy',np.array(y,dtype=np.int64))
            (data/'config.json').write_text(json.dumps(CONFIG))
            with (data/'metadata.csv').open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(metadata[0]));writer.writeheader();writer.writerows(metadata)
            fallback.register();lab.train(data,root/'lab',fallback.PROFILE,[42],1)
            fallback.export(root/'lab/seed_42',root/'native')
            p=Predictor(root/'native');a=np.array(x[:3]);b=a.copy();a[:,:,9]=0;b[:,:,9]=1
            np.testing.assert_allclose(p.probabilities(a),p.probabilities(b),atol=1e-7)
            self.assertTrue(np.all(p.model.state_dict()['features.0.weight'][:,9,:].numpy()==0))


if __name__=='__main__':unittest.main()
