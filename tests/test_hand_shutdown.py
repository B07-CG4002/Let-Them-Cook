import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from src.hand_ai_flex3.async_queue import queue_item
from src.hand_ai_flex3.record_both_gui import BothWorker

class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_wins_when_queue_result_is_ready(self):
        future=asyncio.get_running_loop().create_future()
        queue=SimpleNamespace(get=lambda:future)
        task=asyncio.create_task(queue_item(queue))
        await asyncio.sleep(0)
        future.set_result('packet')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task

    async def test_timeout_still_rejects_missing_samples(self):
        with self.assertRaises(TimeoutError):await queue_item(asyncio.Queue(),.001)

    async def test_immediate_data_is_preserved(self):
        q=asyncio.Queue();q.put_nowait(('packet','values','time'))
        self.assertEqual(await queue_item(q),('packet','values','time'))

    async def worker_case(self,recording=False,hot=False):
        connected=asyncio.Event();batch_started=asyncio.Event();closed=[];producers=[]
        @asynccontextmanager
        async def connection(d):
            q=asyncio.Queue();health=dict(dropped=0,malformed=0)
            async def produce():
                while True:
                    await asyncio.sleep(0)
                    q.put_nowait((SimpleNamespace(device_id=d,boot_id=1,seq=1),(0.,)*12,0.))
            producer=asyncio.create_task(produce()) if hot else None
            if producer:producers.append(producer)
            try:yield None,q,health
            finally:
                if producer:
                    producer.cancel();await asyncio.gather(producer,return_exceptions=True)
                closed.append(d)
        class Worker(BothWorker):
            def event(self,kind,**fields):
                super().event(kind,**fields)
                if kind=='connected':connected.set()
            async def batch_both(self,settings):
                batch_started.set();await asyncio.Future()
        worker=Worker((1,2),connection_factory=connection)
        running=asyncio.create_task(worker.supervise())
        try:
            await asyncio.wait_for(connected.wait(),2)
            if recording:
                worker.commands.put(dict(settings={}))
                await asyncio.wait_for(batch_started.wait(),2)
            if hot:
                for _ in range(10):await asyncio.sleep(0)
            worker.cancel_recording.set();worker.stop.set()
            await asyncio.wait_for(running,2)
            self.assertEqual(sorted(closed),[1,2])
            self.assertTrue(all(t.done() for t in producers))
        finally:
            if not running.done():running.cancel()
            await asyncio.gather(running,return_exceptions=True)

    async def test_idle_two_hand_close_reaches_both_context_cleanups(self):
        await self.worker_case()

    async def test_active_batch_close_is_not_swallowed_as_batch_cancel(self):
        await self.worker_case(recording=True)

    async def test_busy_stream_close_still_reaches_cleanup(self):
        await self.worker_case(hot=True)

if __name__=='__main__':unittest.main()
