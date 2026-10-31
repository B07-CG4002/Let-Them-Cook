import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools import hand_collection as c

class CollectionTests(unittest.TestCase):
    def test_stable_session_and_split(self):
        self.assertEqual(c.session_name('P02','20261030','repaired_v1','validation'),'P02_repaired_v1_20261030_validation')
        self.assertNotEqual(c.session_name('P02','20261030','repaired_v1','test'),c.session_name('P02','20261030','repaired_v1','train'))
        with self.assertRaises(ValueError):c.session_name('../P02','20261030','v1','train')
    def test_resume_clamps_target_and_refuses_orphan_or_tamper(self):
        self.assertEqual(c.remaining(30,27,10),3)
        self.assertEqual(c.remaining(30,30,30),0)
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'P01_session_PICKUP_001.csv';p.write_text('raw')
            with self.assertRaises(ValueError):c.completed(t,'P01','session','PICKUP')
            p.with_suffix('.json').write_text(json.dumps(dict(person='P01',session='session',gesture='PICKUP',csv_sha256=hashlib.sha256(p.read_bytes()).hexdigest())))
            self.assertEqual(c.completed(t,'P01','session','PICKUP'),1)
            p.write_text('changed')
            with self.assertRaises(ValueError):c.completed(t,'P01','session','PICKUP')
    def test_launcher_waits_and_orders_stages_and_reports_failure(self):
        calls=[]
        with patch.object(c.subprocess,'run',side_effect=lambda args:(calls.append(args) or SimpleNamespace(returncode=0))):
            self.assertEqual(c.main(['--person','P01','--date','20261030']),0)
        self.assertEqual([a[a.index('--stage')+1] for a in calls],['train','validation','test','diagnostic'])
        with patch.object(c.subprocess,'run',return_value=SimpleNamespace(returncode=2)) as run:
            self.assertEqual(c.main(['--person','P01','--date','20261030','--stage','validation']),2)
            self.assertEqual(run.call_count,1)
    def test_shutdown_waits_for_real_thread_before_destroy(self):
        from src.hand_ai_flex3.record_both_gui import App
        calls=[];alive=[True]
        obj=SimpleNamespace(worker=SimpleNamespace(thread=SimpleNamespace(is_alive=lambda:alive[0],join=lambda:calls.append('join'))),root=SimpleNamespace(after=lambda *args:calls.append('wait'),destroy=lambda:calls.append('destroy')),finish_close=lambda:None)
        App.finish_close(obj);self.assertEqual(calls,['wait'])
        alive[0]=False;App.finish_close(obj);self.assertEqual(calls,['wait','join','destroy'])

class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def capture_fixture(self,folder,*,samples=150):
        """Synthetic callback times; release the duration timer only after both readers drain.

        No sample production depends on a sub-second Windows sleep deadline.
        The recorder's sample-count/span/hash/source checks are still real.
        """
        from common.sensor_v5 import SensorPacket
        class ObservedQueue(asyncio.Queue):
            def __init__(self):
                super().__init__();self.requests=0;self.drained=asyncio.Event()
            async def get(self):
                self.requests+=1
                if self.requests==samples+1:self.drained.set()
                return await super().get()
        links={d:(ObservedQueue(),dict(dropped=0,malformed=0)) for d in (1,2)}
        release=asyncio.Event();timer_entered=asyncio.Event()
        class ControlledAsyncio:
            def __getattr__(self,name):return getattr(asyncio,name)
            async def sleep(self,seconds):
                self_seconds=seconds
                if self_seconds!=3:raise AssertionError('Fixture expects a real 3-second clip configuration')
                timer_entered.set();await release.wait()
        meta=dict(person='P01',session='edge',case='lp_rd',repetition=1,intent={'LEFT':'PICKUP','RIGHT':'DROP'})
        with patch.object(c,'asyncio',ControlledAsyncio()),patch.object(c,'time',SimpleNamespace(monotonic=lambda:1000.,time_ns=time.time_ns)):
            job=asyncio.create_task(c.capture_clip(folder,meta,links,3))
            try:
                await asyncio.wait_for(timer_entered.wait(),10)
                # Simulate scheduler delay longer than the old entire 80-ms deadline.
                await asyncio.sleep(.12)
                self.assertFalse(job.done())
                for seq in range(1,samples+1):
                    for d in (1,2):
                        links[d][0].put_nowait((SensorPacket(d,1,seq,seq*20,(0,)*12),(0.,)*12,1000.+seq*.02))
                await asyncio.wait_for(asyncio.gather(*(q.drained.wait() for q,_ in links.values())),10)
                self.assertFalse(job.done())
                release.set()
                return await asyncio.wait_for(job,10)
            finally:
                if not job.done():job.cancel()
                await asyncio.gather(job,return_exceptions=True)

    async def test_complete_capture_preserves_both_hands_hashes_and_annotations(self):
        from src.hand_ai_lab.data import load_capture
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)/'001';await self.capture_fixture(folder)
            self.assertTrue(c.edge_done(folder))
            header,rows=load_capture(folder/'capture.jsonl')
            self.assertEqual(header['split'],'diagnostic')
            for d in (1,2):
                selected=[r for r in rows if r['device_id']==d]
                self.assertEqual(len(selected),150)
                self.assertEqual([r['seq'] for r in selected],list(range(1,151)))
                self.assertAlmostEqual(selected[-1]['elapsed_s']-selected[0]['elapsed_s'],2.98)
            a=json.loads((folder/'annotations_template.json').read_text())
            self.assertFalse(a['reviewed']);self.assertEqual(a['segments'],[])

    async def test_insufficient_coverage_is_still_rejected_and_retained(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)/'001'
            with self.assertRaisesRegex(ValueError,'insufficient continuous coverage'):
                await self.capture_fixture(folder,samples=70)
            self.assertFalse(folder.exists())
            partial=next(Path(t).glob('001.partial_*'))
            self.assertEqual(json.loads((partial/'status.json').read_text())['status'],'partial')
            self.assertTrue((partial/'capture_receive_order.jsonl').is_file())

    async def test_interruption_retains_partial_evidence(self):
        waiting=asyncio.Event()
        class ObservedQueue(asyncio.Queue):
            async def get(self):waiting.set();return await super().get()
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)/'001'
            links={d:(ObservedQueue(),dict(dropped=0,malformed=0)) for d in (1,2)}
            meta=dict(person='P01',session='edge',case='lp_rd',repetition=1,intent={})
            job=asyncio.create_task(c.capture_clip(folder,meta,links,3))
            try:
                await asyncio.wait_for(waiting.wait(),10);job.cancel()
                with self.assertRaises(asyncio.CancelledError):await job
            finally:
                if not job.done():job.cancel()
                await asyncio.gather(job,return_exceptions=True)
            self.assertFalse(folder.exists());self.assertTrue(list(Path(t).glob('001.partial_*/status.json')))

if __name__=='__main__':unittest.main()
