"""Event-driven capture tests; do not assert a mock packet-drain rate."""
import asyncio
from contextlib import asynccontextmanager
import csv, json, struct, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from src.hand_ai_flex3 import triage_capture as triage
from src.hand_ai_flex3.sensor import SensorPacket, physical_values

class TriageTests(unittest.TestCase):
    def test_explicit_feature_projection_preserves_full_raw_input(self):
        a=np.arange(600).reshape(50,12); original=a.copy()
        np.testing.assert_array_equal(triage.model_window(a,True),a[:,:11])
        self.assertEqual(triage.model_window(a,False).shape,(50,12))
        np.testing.assert_array_equal(a,original)
        with self.assertRaises(ValueError):triage.model_window(a[:,:11],True)

class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, yield_steps=0, packet_delay=0., inject_gap=False):
        total=151; closed=[]; written={1:0,2:0}
        samples_written=asyncio.Event(); prediction_written=asyncio.Event()
        barrier=31.23456789
        # Freeze only diagnostic timestamps. asyncio retains its real clock.
        clock=SimpleNamespace(monotonic=lambda:100.,time=lambda:1700000000.)
        original_window=triage.HandWindow; original_json=triage.row_json

        class ObservedWindow(original_window):
            def add(self,packet,values,received):
                super().add(packet,values,received)
                # The real CSV row is written before the window is updated.
                written[packet.device_id]+=1
                if all(n==total for n in written.values()):samples_written.set()

        def observe_json(stream,obj):
            original_json(stream,obj)
            if 'probabilities' in obj:prediction_written.set()

        class ControlledAsyncio:
            def __getattr__(self,name):return getattr(asyncio,name)
            async def sleep(self,seconds):
                if seconds==barrier:
                    # Completion depends on work observed, not a 0.6s deadline.
                    await asyncio.wait_for(samples_written.wait(),30)
                    await asyncio.wait_for(prediction_written.wait(),30)
                else:await asyncio.sleep(0)
            async def wait_for(self,awaitable,timeout):
                # The finite mock stream ends after 151 samples. A mock hand
                # must not hit its no-data watchdog while the other drains.
                return await asyncio.wait_for(awaitable,30 if timeout==3 else timeout)

        class PacketQueue(asyncio.Queue):
            async def get(self):
                if packet_delay:
                    await asyncio.sleep(packet_delay)
                for _ in range(yield_steps):await asyncio.sleep(0)
                return await super().get()

        class Client:
            def __init__(self,device):self.device=device
            async def read_gatt_char(self,*args,**kwargs):
                return struct.pack('<4sBB2xIIIIIII',b'LCS4',self.device,2,7,total,total,0,0,0,0)

        @asynccontextmanager
        async def connect(device,address):
            queue=PacketQueue()
            for i in range(total):
                seq=i+int(inject_gap and i>=20)
                packet=SensorPacket(device,7,seq,i*20,tuple(i+j for j in range(12)))
                queue.put_nowait((packet,physical_values(packet.values),100.))
            try:yield Client(device),queue,dict(dropped=0,malformed=0)
            finally:closed.append(device)

        class Predictor:
            device_id=2
            torch=SimpleNamespace(set_num_threads=lambda n:None)
            def __init__(self,path):pass
            def probabilities(self,x):
                assert x.shape==(50,11)
                return np.array([[.01,.01,.94,.01,.01,.01,.01]])

        with tempfile.TemporaryDirectory() as d:
            model=Path(d)/'model';model.mkdir()
            (model/'config.json').write_text('{}',encoding='utf-8')
            args=SimpleNamespace(out=str(Path(d)/'captures'),hand='both',run=str(model),legacy11=True)
            modules={'src.hand_ai.pipeline':SimpleNamespace(Predictor=Predictor),
                     'winsound':SimpleNamespace(Beep=lambda *args:None)}
            with patch.dict('sys.modules',modules), \
                 patch.object(triage,'connection',connect), \
                 patch.object(triage,'time',clock), \
                 patch.object(triage,'asyncio',ControlledAsyncio()), \
                 patch.object(triage,'HandWindow',ObservedWindow), \
                 patch.object(triage,'row_json',observe_json), \
                 patch.object(triage,'PLAN',[('IDLE',0),('CHOP',barrier)]), \
                 patch('builtins.print'):
                await asyncio.wait_for(triage.run(args),40)

            root=next((Path(d)/'captures').iterdir())
            summary=json.loads((root/'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(summary['status'],'captured')
            self.assertEqual(sorted(closed),[1,2])
            self.assertTrue(samples_written.is_set());self.assertTrue(prediction_written.is_set())
            for device,hand in ((1,'left'),(2,'right')):
                with (root/f'{hand}_raw.csv').open(newline='',encoding='utf-8') as stream:
                    rows=list(csv.DictReader(stream))
                self.assertEqual(len(rows),total);self.assertEqual(len(rows[0]),18)
                self.assertIn('flex3',rows[0])
                expected_seq=[i+int(inject_gap and i>=20) for i in range(total)]
                self.assertEqual([int(row['seq']) for row in rows],expected_seq)
                self.assertTrue(all(int(row['device_id'])==device for row in rows))
                self.assertAlmostEqual(float(rows[-1]['flex3']),161/10000)
                counts=summary['hands'][hand.upper()]['counts']
                self.assertEqual(counts['packets'],total)
                self.assertEqual(counts['sequence_discontinuities'],int(inject_gap))
            self.assertEqual(len((root/'cues.jsonl').read_text(encoding='utf-8').splitlines()),2)
            results=[json.loads(line) for line in (root/'predictions.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue(results)
            self.assertTrue(all(row['device_id']==2 and row['gesture']=='CHOP' for row in results))
            self.assertFalse(summary['cue_labels_are_frame_ground_truth'])

    async def test_capture_writes_both_raw_streams_cues_predictions_and_closes(self):
        await self.exercise()
    async def test_capture_preserves_all_samples_with_interleaved_mock_receivers(self):
        await self.exercise(yield_steps=5)
    async def test_capture_reports_sequence_gap_without_dropping_other_samples(self):
        await self.exercise(yield_steps=2,inject_gap=True)
    async def test_slow_mock_stream_finishes_after_all_packets_are_written(self):
        await self.exercise(packet_delay=.006)

if __name__=='__main__':unittest.main()
