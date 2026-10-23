"""Headless GUI logic tests; no display, Bluetooth, or glove required."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from common.sensor import SensorPacket
from src.hand_ai.record_gui import settings, prepare_session, save_trial, Worker

class SettingsTests(unittest.TestCase):
    def test_select_left_and_right(self):
        for hand,device in [('LEFT',1),('RIGHT',2)]:
            a=settings('P01','pilot1','train',hand,'PICKUP','3','data/hand_pilot')
            self.assertEqual(a['device_id'],device)
            self.assertEqual(a['root'],Path('data/hand_pilot')/hand.lower()/'pilot1')
    def test_invalid_count_and_identity(self):
        for count in ('0','1.5','abc'):
            with self.assertRaises(ValueError):settings('P01','s1','train','RIGHT','PICKUP',count,'data')
        with self.assertRaises(ValueError):settings('../P01','s1','train','RIGHT','PICKUP','1','data')
    def test_save_schema_auto_number_and_session_guard(self):
        from src.hand_ai.core import digest
        with tempfile.TemporaryDirectory() as d:
            a=settings('P01','pilot1','train','RIGHT','PICKUP','2',d)
            self.assertEqual(prepare_session(a),[1,2])
            rows=[(i*.02,SensorPacket(2,7,i,i*20,(0,)*11),(0.,)*11) for i in range(101)]
            path,n,duration,saturated=save_trial(a,1,rows)
            self.assertEqual(n,101);self.assertEqual(duration,2.)
            self.assertEqual(prepare_session(a),[2,3])
            meta=json.loads(path.with_suffix('.json').read_text())
            self.assertEqual(meta['csv_sha256'],digest(path))
            self.assertEqual(meta['device_id'],2)
            with self.assertRaises(FileExistsError):save_trial(a,1,rows)
            a['split']='test'
            with self.assertRaises(ValueError):prepare_session(a)

class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuous_receiver_during_user_preparation(self):
        # This is a functional receive test, not a 3,000-packet speed benchmark.
        # Signal completion explicitly instead of assuming a machine drains in 1s.
        total=128;drained=asyncio.Event();opened=asyncio.Event()
        class ObservedWorker(Worker):
            @property
            def count(self):return self._count
            @count.setter
            def count(self,value):
                self._count=value
                if value==total:drained.set()
        incoming=asyncio.Queue();health=dict(dropped=0,malformed=0)
        worker=ObservedWorker(2)
        @asynccontextmanager
        async def fake_connection(device):
            opened.set()
            yield None,incoming,health
        with patch('src.hand_ai.record_gui.connection',fake_connection):
            task=asyncio.create_task(worker.session())
            try:
                await asyncio.wait_for(opened.wait(),30)
                for i in range(total):
                    incoming.put_nowait((SensorPacket(2,7,i,i*20,(0,)*11),(0.,)*11,0.))
                # Long timeout is only a deadlock guard, never an asserted rate.
                await asyncio.wait_for(drained.wait(),30)
                self.assertEqual(worker.count,total)
                self.assertTrue(incoming.empty())
            finally:
                worker.stop.set()
                try:await asyncio.wait_for(task,10)
                finally:
                    if not task.done():task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
    async def test_reader_errors_are_propagated(self):
        worker=Worker(2);incoming=asyncio.Queue();health=dict(dropped=1,malformed=0)
        @asynccontextmanager
        async def fake_connection(device):yield None,incoming,health
        with patch('src.hand_ai.record_gui.connection',fake_connection):
            task=asyncio.create_task(worker.session())
            await incoming.put((SensorPacket(2,7,0,0,(0,)*11),(0.,)*11,0.))
            with self.assertRaises(ValueError):await asyncio.wait_for(task,2)


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_batch_saves_source_timestamped_trial(self):
        from src.hand_ai.ble import NotificationQueue
        health=dict(dropped=0,malformed=0)
        inbox=NotificationQueue(2,7,health,trial_mode=True)
        worker=Worker(2)
        async def feed():
            while not inbox.accepting:await asyncio.sleep(0)
            for i in range(101):
                inbox.put_nowait((SensorPacket(2,7,i,i*20,(0,)*11),(0.,)*11,100.))
        with tempfile.TemporaryDirectory() as d:
            a=settings('P01','pilot1','train','RIGHT','PICKUP','1',d)
            # Isolate sensor freshness from OS/debug-mode scheduling. Replace
            # only ble's time namespace; asyncio retains its real timeout clock.
            with patch('src.hand_ai.record_gui.WAIT_SECONDS',0), \
                 patch('src.hand_ai.ble.time',SimpleNamespace(monotonic=lambda:100.)):
                feeder=asyncio.create_task(feed())
                try:
                    await asyncio.wait_for(worker.batch(a,inbox,health),30)
                    await feeder
                finally:
                    if not feeder.done():feeder.cancel()
                    await asyncio.gather(feeder,return_exceptions=True)
            events,_,_=worker.snapshot()
            self.assertTrue(any(e['kind']=='complete' for e in events))
            self.assertEqual(len(list(a['root'].glob('*.csv'))),1)
            self.assertFalse(inbox.accepting)


class CancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_batch_keeps_powered_connection_and_can_restart(self):
        worker=Worker(2);incoming=asyncio.Queue();health=dict(dropped=0,malformed=0)
        opened=asyncio.Event();closed=asyncio.Event();started=asyncio.Event();ended=asyncio.Event()
        @asynccontextmanager
        async def fake_connection(device):
            opened.set()
            try:yield None,incoming,health
            finally:closed.set()
        async def fake_batch(a,q,h):
            started.set()
            try:await asyncio.Event().wait()
            finally:ended.set()
        with patch('src.hand_ai.record_gui.connection',fake_connection),patch.object(worker,'batch',fake_batch):
            task=asyncio.create_task(worker.supervise());await opened.wait()
            worker.commands.put(dict(kind='record',settings={}))
            await started.wait();worker.cancel_recording.set()
            await asyncio.wait_for(ended.wait(),1)
            await asyncio.sleep(.05)
            self.assertFalse(closed.is_set());self.assertFalse(task.done())
            events,_,_=worker.snapshot();self.assertTrue(any(e['kind']=='cancelled' for e in events))
            started.clear();ended.clear();worker.cancel_recording.clear()
            worker.commands.put(dict(kind='record',settings={}))
            await asyncio.wait_for(started.wait(),1)
            worker.stop.set();await asyncio.wait_for(task,1)
            self.assertTrue(closed.is_set());self.assertTrue(ended.is_set())
    async def test_disconnect_while_connecting(self):
        worker=Worker(2);started=asyncio.Event();closed=asyncio.Event()
        @asynccontextmanager
        async def fake_connection(device):
            started.set()
            try:
                await asyncio.Event().wait()
                yield None,None,None
            finally:closed.set()
        with patch('src.hand_ai.record_gui.connection',fake_connection):
            task=asyncio.create_task(worker.supervise());await started.wait()
            worker.stop.set();await asyncio.wait_for(task,1)
            self.assertTrue(closed.is_set())
    async def test_disconnect_during_idle_receive(self):
        worker=Worker(2);opened=asyncio.Event();closed=asyncio.Event()
        @asynccontextmanager
        async def fake_connection(device):
            opened.set()
            try:yield None,asyncio.Queue(),dict(dropped=0,malformed=0)
            finally:closed.set()
        with patch('src.hand_ai.record_gui.connection',fake_connection):
            task=asyncio.create_task(worker.supervise());await opened.wait()
            worker.stop.set();await asyncio.wait_for(task,1)
            self.assertTrue(closed.is_set())

class DisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_disconnect_without_stop_notify(self):
        from src.hand_ai.ble import close_client
        class Client:
            is_connected=True
            async def disconnect(self):self.is_connected=False
            async def stop_notify(self,*args):raise AssertionError('Must not block on CCCD write')
        c=Client();await close_client(c,.1);self.assertFalse(c.is_connected)
    async def test_disconnect_timeout_reported(self):
        from src.hand_ai.ble import close_client
        class Client:
            is_connected=True
            async def disconnect(self):await asyncio.Event().wait()
        with self.assertRaisesRegex(TimeoutError,'unconfirmed'):await close_client(Client(),.01)
    async def test_still_connected_is_not_reported_as_success(self):
        from src.hand_ai.ble import close_client
        class Client:
            is_connected=True
            async def disconnect(self):pass
        with self.assertRaises(ConnectionError):await close_client(Client(),.1)

if __name__=='__main__':unittest.main()
