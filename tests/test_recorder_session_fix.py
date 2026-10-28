import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from common.sensor_v5 import SensorPacket, physical_values
from src.hand_ai_flex3 import record_both_gui as dual
from src.hand_ai_flex3.wide_record_gui import WideWorker


def config(root, session='train_session', split='train', devices=(1,2)):
    pairs=dual.pair_settings('P01',session,split,'PICKUP','PICKUP',1,str(root))
    return WideWorker(devices).prepare_config({d:pairs[d] for d in devices})


class SettingsTests(unittest.TestCase):
    def test_split_change_is_read_only_and_preserves_completed_training(self):
        with TemporaryDirectory() as temp:
            original=config(temp)
            for a in original.values():
                dual.prepare_session(a)
                (a['root']/'completed_trial.csv').write_bytes(b'keep my training data\r\n')
            before={str(p):p.read_bytes() for p in Path(temp).rglob('*') if p.is_file()}
            for split in ('validation','test'):
                with self.assertRaisesRegex(dual.SessionConflict,'already belongs to split "train"'):
                    dual.validate_session_settings(config(temp,split=split))
                self.assertEqual(before,{str(p):p.read_bytes() for p in Path(temp).rglob('*') if p.is_file()})

    def test_matching_and_separate_v5_sessions_are_accepted_without_writes(self):
        with TemporaryDirectory() as temp:
            a=config(temp)
            for v in a.values():dual.prepare_session(v)
            dual.validate_session_settings(a)
            for split in ('validation','test'):
                next_config=config(temp,session=split+'_session',split=split)
                dual.validate_session_settings(next_config)
                self.assertFalse(next_config[1]['root'].exists())
                self.assertFalse(next_config[2]['root'].exists())

    def test_gui_rejects_conflict_before_queue_or_busy_changes(self):
        with TemporaryDirectory() as temp:
            for v in config(temp).values():dual.prepare_session(v)
            values=dict(person='P01',session='train_session',split='validation',left='PICKUP',right='PICKUP',count='4',output=temp)
            app=SimpleNamespace(vars={k:SimpleNamespace(get=lambda v=v:v) for k,v in values.items()},worker=WideWorker(),busy=False,controls=Mock())
            with patch.object(dual.messagebox,'showerror') as dialog:
                dual.App.start(app)
            dialog.assert_called_once()
            self.assertFalse(app.busy)
            self.assertTrue(app.worker.commands.empty())
            self.assertFalse(app.worker.stop.is_set())

    def test_only_selected_hand_checked_and_corrupt_marker_refused(self):
        with TemporaryDirectory() as temp:
            for v in config(temp,devices=(2,)).values():dual.prepare_session(v)
            dual.validate_session_settings(config(temp,split='validation',devices=(1,)))
            marker=config(temp,devices=(2,))[2]['root']/'session.json'
            marker.write_text('{broken')
            with self.assertRaisesRegex(dual.SessionConflict,'Cannot read session marker'):
                dual.validate_session_settings(config(temp,devices=(2,)))


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_conflict_keeps_links_then_records_validation_and_test(self):
        opened=[];closed=[]
        @asynccontextmanager
        async def connection(device):
            opened.append(device)
            try:yield None,asyncio.Queue(),dict(dropped=0,malformed=0)
            finally:closed.append(device)
        async def collect(queue,health):
            rows=[]
            for i in range(101):
                raw=(i,0,0,i,0,0,0,0,0,5000,5000,5000)
                p=SensorPacket(queue.device,42,i,i*20,raw)
                rows.append((i*.02,p,physical_values(raw)))
            return rows
        async def until(predicate):
            while not predicate():await asyncio.sleep(.005)
        with TemporaryDirectory() as temp:
            for a in config(temp).values():dual.prepare_session(a)
            worker=WideWorker();worker.connection_factory=connection
            with patch.object(dual,'collect',collect),patch.object(dual,'WAIT_SECONDS',0):
                task=asyncio.create_task(worker.supervise())
                try:
                    for split in ('validation','test'):
                        worker.events.clear()
                        worker.commands.put(dict(settings=config(temp,split=split)))
                        await asyncio.wait_for(until(lambda:any(e['kind']=='batch_error' for e in worker.events)),2)
                        self.assertFalse(task.done());self.assertEqual(closed,[])
                        worker.events.clear()
                        worker.commands.put(dict(settings=config(temp,session=split+'_session',split=split)))
                        await asyncio.wait_for(until(lambda:any(e['kind']=='complete' for e in worker.events)),2)
                        for device in (1,2):
                            folder=config(temp,session=split+'_session',split=split)[device]['root']
                            path=next(folder.glob('*.csv'))
                            metadata=json.loads(path.with_suffix('.json').read_text())
                            self.assertEqual(metadata['split'],split)
                            self.assertEqual(metadata['recording_contract'],'independent_hand_flex3_gyro_v5_v1')
                    self.assertEqual(opened,[1,2]);self.assertEqual(closed,[])
                finally:
                    worker.stop.set();await asyncio.wait_for(task,2)
            self.assertEqual(set(closed),{1,2})

    async def test_second_hand_conflict_creates_no_first_hand_folder(self):
        with TemporaryDirectory() as temp:
            right=config(temp,devices=(2,))[2];dual.prepare_session(right)
            worker=WideWorker()
            with self.assertRaises(dual.SessionConflict):
                await worker.batch_both(config(temp,split='test'))
            self.assertFalse(config(temp)[1]['root'].exists())


if __name__=='__main__':unittest.main()
