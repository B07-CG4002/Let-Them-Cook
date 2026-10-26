import asyncio
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
import numpy as np
from src.hand_ai_flex3.core import LABELS, CHANNELS, resample, fit_normalizer, validate_ratios, write_json, digest
from src.hand_ai_flex3.pipeline import prepare, load_data
from src.hand_ai_flex3.ble import collect
from src.hand_ai_flex3.sensor import SensorPacket

class CoreTests(unittest.TestCase):
    def test_angles_take_short_path(self):
        v=np.zeros((2,12));v[:,6]=[179.,-179.]
        out=resample([0.,.02],v)
        self.assertTrue((np.abs(out[:,6])>=179).all())
        self.assertEqual(out.shape,(50,12))
    def test_reject_gaps_and_nonfinite(self):
        for t in ([0.,.101],[0.,0.]):
            with self.assertRaises(ValueError):resample(t,np.zeros((2,12)))
        v=np.zeros((2,12));v[0,0]=np.nan
        with self.assertRaises(ValueError):resample([0.,.02],v)
    def test_normalization_constant_channel(self):
        mean,std=fit_normalizer(np.ones((2,50,12))*3)
        np.testing.assert_allclose(mean,3);self.assertTrue((std>0).all())
    def test_ratios_and_session_leak(self):
        rows=[]
        for g in LABELS:
            for s,n in [('train',30),('validation',4),('test',6)]:
                rows.extend(dict(gesture=g,split=s,person='P1',session=s) for _ in range(n))
        validate_ratios(rows);rows[-1]['session']='train'
        with self.assertRaises(ValueError):validate_ratios(rows)
    def test_rounded_20_3_4_split(self):
        rows=[]
        for g in LABELS:
            for split,n in [('train',20),('validation',3),('test',4)]:
                rows.extend(dict(gesture=g,split=split,person='P02',session=split) for _ in range(n))
        counts=validate_ratios(rows)
        self.assertEqual(counts['CHOP'],dict(train=20,validation=3,test=4))
        rows[-1]['split']='validation';rows[-1]['session']='validation'
        with self.assertRaisesRegex(ValueError,'rounded'):validate_ratios(rows)
    def test_output_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.json';write_json(p,dict(x=1))
            with self.assertRaises(FileExistsError):write_json(p,dict(x=2))

class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_batched_source_times(self):
        q=asyncio.Queue()
        async def feed():
            await asyncio.sleep(0)
            for i in range(101):q.put_nowait((SensorPacket(2,7,i,i*20,(0,)*12),(0.,)*12,100.))
        # Replace this module's clock object only. Patching time.monotonic itself
        # would also change asyncio's clock and could break its timeout handling.
        with patch('src.hand_ai_flex3.ble.time', SimpleNamespace(monotonic=lambda:100.)):
            task=asyncio.create_task(feed())
            try: rows=await collect(q,dict(dropped=0,malformed=0))
            finally: await task
        self.assertEqual(len(rows),101);self.assertEqual(rows[-1][0],2.)
    async def test_collect_rejects_sequence_gap(self):
        q=asyncio.Queue()
        async def feed():
            await asyncio.sleep(0)
            for i in (0,2):q.put_nowait((SensorPacket(2,7,i,i*20,(0,)*12),(0.,)*12,100.))
        with patch('src.hand_ai_flex3.ble.time', SimpleNamespace(monotonic=lambda:100.)):
            task=asyncio.create_task(feed())
            try:
                with self.assertRaisesRegex(ValueError,'sequence gap'):
                    await collect(q,dict(dropped=0,malformed=0))
            finally: await task
    async def test_collect_still_rejects_stale_samples(self):
        q=asyncio.Queue()
        async def feed():
            await asyncio.sleep(0)
            q.put_nowait((SensorPacket(2,7,0,0,(0,)*12),(0.,)*12,100.))
        # Capture starts at 100; processing at 101 makes the received sample stale.
        clock=Mock(side_effect=[100.,101.])
        with patch('src.hand_ai_flex3.ble.time', SimpleNamespace(monotonic=clock)):
            task=asyncio.create_task(feed())
            try:
                with self.assertRaisesRegex(ValueError,'Stale queued samples'):
                    await collect(q,dict(dropped=0,malformed=0))
            finally: await task
    async def test_collect_ignores_pre_cue_samples(self):
        q=asyncio.Queue()
        async def feed():
            await asyncio.sleep(0)
            q.put_nowait((SensorPacket(2,7,500,10000,(0,)*12),(0.,)*12,99.))
            for i in range(101):q.put_nowait((SensorPacket(2,7,i,i*20,(0,)*12),(0.,)*12,100.))
        with patch('src.hand_ai_flex3.ble.time', SimpleNamespace(monotonic=lambda:100.)):
            task=asyncio.create_task(feed())
            try: rows=await collect(q,dict(dropped=0,malformed=0))
            finally: await task
        self.assertEqual(rows[0][1].seq,0)
        self.assertEqual(len(rows),101)

def make_dataset(root):
    n=0
    for g in LABELS:
        for split,count in [('train',15),('validation',2),('test',3)]:
            folder=root/split;folder.mkdir(exist_ok=True,parents=True)
            for trial in range(count):
                n+=1;path=folder/f'{g}_{trial}.csv';t=np.arange(101)*.02
                values=np.sin(t[:,None]*np.arange(1,13)[None,:]+n*.013)
                with path.open('w',newline='') as f:
                    w=csv.writer(f);w.writerow(['elapsed_s','device_id','boot_id','seq','uptime_ms',*CHANNELS])
                    for i in range(101):w.writerow([t[i],2,7,i,i*20,*values[i]])
                write_json(path.with_suffix('.json'),dict(person='P1',session=split,split=split,
                    gesture=g,trial=trial,device_id=2,recording_contract='independent_hand_flex3_v1',csv_sha256=digest(path)))

class PipelineTests(unittest.TestCase):
    def test_prepare_and_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'raw';out=Path(d)/'prepared';make_dataset(root)
            prepare(SimpleNamespace(input=str(root),out=str(out),device=2));x,y,meta=load_data(out)
            self.assertEqual(x.shape,(140,50,12))
            self.assertEqual(meta.split.value_counts().to_dict(),dict(train=105,validation=14,test=21))
            p=next(root.rglob('*.csv'));p.write_text(p.read_text()+'\n')
            with self.assertRaisesRegex(ValueError,'changed'):
                prepare(SimpleNamespace(input=str(root),out=str(Path(d)/'bad'),device=2))
    @unittest.skipUnless(importlib.util.find_spec('torch'),'PyTorch not installed')
    def test_train_evaluate_predict(self):
        from src.hand_ai_flex3.pipeline import train,evaluate,Predictor
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'raw';data=Path(d)/'prepared';run=Path(d)/'model';make_dataset(root)
            prepare(SimpleNamespace(input=str(root),out=str(data),device=2))
            train(SimpleNamespace(data=str(data),run=str(run),epochs=2,seed=42))
            out=Path(d)/'evaluation.json'
            evaluate(SimpleNamespace(run=str(run),data=None,split='test',out=str(out)))
            self.assertEqual(json.loads(out.read_text())['samples'],21)
            prob=Predictor(run).probabilities(np.load(data/'X.npy')[:2])
            self.assertEqual(prob.shape,(2,7));np.testing.assert_allclose(prob.sum(1),1,atol=1e-5)
            with self.assertRaises(FileExistsError):evaluate(SimpleNamespace(run=str(run),data=None,split='test',out=str(out)))

class WindowTests(unittest.TestCase):
    def test_live_window_and_reset(self):
        from src.hand_ai_flex3.live import HandWindow
        state=HandWindow()
        for i in range(101):state.add(SensorPacket(2,7,i,i*20,(0,)*12),(0.,)*12,0.)
        self.assertEqual(state.window().shape,(50,12))

        state.add(SensorPacket(2,7,103,2060,(0,)*12),(0.,)*12,0.)
        self.assertIsNone(state.window())
    def test_timestamp_wrap(self):
        from src.hand_ai_flex3.live import HandWindow
        state=HandWindow()
        for i in range(101):state.add(SensorPacket(2,7,i,(2**32-1000+i*20)%2**32,(0,)*12),(0.,)*12,0.)
        self.assertEqual(state.window().shape,(50,12))


class TrialQueueTests(unittest.IsolatedAsyncioTestCase):
    def make_queue(self, **kwargs):
        from src.hand_ai_flex3.ble import NotificationQueue
        health=dict(dropped=0,malformed=0)
        return NotificationQueue(2,7,health,**kwargs),health
    def payload(self,i=0):
        from src.hand_ai_flex3.sensor import encode_packet
        return encode_packet(SensorPacket(2,7,i,i*20,(0,)*12))
    async def test_long_preparation_does_not_fill_queue(self):
        q,h=self.make_queue(trial_mode=True,maxsize=2)
        for i in range(5000):q.feed(self.payload(i),99.)
        self.assertTrue(q.empty());self.assertEqual(h,dict(dropped=0,malformed=0))
        q.start_trial(100.)
        q.feed(self.payload(0),99.)
        self.assertTrue(q.empty())
        q.feed(self.payload(0),100.)
        self.assertEqual(q.qsize(),1)
        q.stop_trial()
        for i in range(5000):q.feed(self.payload(i),101.)
        self.assertTrue(q.empty());self.assertEqual(h,dict(dropped=0,malformed=0))
    async def test_actual_trial_overflow_remains_an_error(self):
        from src.hand_ai_flex3.ble import ensure_healthy
        q,h=self.make_queue(trial_mode=True,maxsize=1);q.start_trial(100.)
        q.feed(self.payload(0),100.);q.feed(self.payload(1),100.)
        with self.assertRaises(ValueError):ensure_healthy(h)
    async def test_malformed_trial_packet_remains_an_error(self):
        from src.hand_ai_flex3.ble import ensure_healthy
        q,h=self.make_queue(trial_mode=True);q.start_trial(100.)
        q.feed(b'broken',100.)
        with self.assertRaises(ValueError):ensure_healthy(h)
    async def test_live_mode_still_receives_without_trial(self):
        q,h=self.make_queue();q.feed(self.payload(),100.)
        self.assertEqual(q.qsize(),1);self.assertEqual(h,dict(dropped=0,malformed=0))
    async def test_repeat_trials_after_long_wait(self):
        q,h=self.make_queue(trial_mode=True)
        async def feed(offset):
            await asyncio.sleep(0)
            for i in range(101):q.feed(self.payload(offset+i),100.)
        with patch('src.hand_ai_flex3.ble.time',SimpleNamespace(monotonic=lambda:100.)):
            for offset in (0,1000):
                for i in range(5000):q.feed(self.payload(i),99.)
                task=asyncio.create_task(feed(offset))
                try:rows=await collect(q,h)
                finally:await task
                self.assertEqual(len(rows),101)
                self.assertFalse(q.accepting);self.assertTrue(q.empty())
    async def test_failure_disarms_trial(self):
        q,h=self.make_queue(trial_mode=True)
        async def feed():
            await asyncio.sleep(0)
            q.feed(self.payload(0),100.);q.feed(self.payload(2),100.)
        with patch('src.hand_ai_flex3.ble.time',SimpleNamespace(monotonic=lambda:100.)):
            task=asyncio.create_task(feed())
            try:
                with self.assertRaisesRegex(ValueError,'sequence gap'):await collect(q,h)
            finally:await task
        self.assertFalse(q.accepting)
        self.assertTrue(q.empty())

if __name__=='__main__':unittest.main()
