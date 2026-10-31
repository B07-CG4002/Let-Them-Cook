import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch
from src.hand_ai_flex3 import wide_ble as wb
from test_wide_gyro import FakeClient


class WindowsLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonwindows_does_not_request_windows_api(self):
        with patch.object(wb,'make_windows_request',side_effect=AssertionError('unexpected Windows call')):
            self.assertIsNone(await wb.windows_throughput(object(),1,_platform='linux'))

    async def test_delayed_fast_interval_holds_request_until_caller_closes(self):
        closed=[];states=iter((48,48,12))
        native=SimpleNamespace(get_connection_parameters=lambda:SimpleNamespace(connection_interval=next(states),connection_latency=0))
        request=SimpleNamespace(close=lambda:closed.append(True))
        client=SimpleNamespace(_backend=SimpleNamespace(_requester=native))
        async def no_wait(_):pass
        with patch.object(wb,'make_windows_request',return_value=request),patch.object(wb.asyncio,'sleep',new=no_wait):
            result=await wb.windows_throughput(client,1,_platform='win32')
        self.assertIs(result,request);self.assertEqual(closed,[])
        result.close();self.assertEqual(closed,[True])

    async def test_timeout_closes_request_and_rejects_slow_link(self):
        closed=[];request=SimpleNamespace(close=lambda:closed.append(True))
        native=SimpleNamespace(get_connection_parameters=lambda:SimpleNamespace(connection_interval=48,connection_latency=0))
        client=SimpleNamespace(_backend=SimpleNamespace(_requester=native))
        with patch.object(wb,'make_windows_request',return_value=request),patch.object(wb,'time',SimpleNamespace(monotonic=iter((0,6)).__next__)):
            with self.assertRaisesRegex(RuntimeError,'60 ms'):
                await wb.windows_throughput(client,1,_platform='win32')
        self.assertEqual(closed,[True])

    async def test_unsupported_backend_is_explicit(self):
        with self.assertRaisesRegex(RuntimeError,'No sensor subscription started'):
            await wb.windows_throughput(object(),1,_platform='win32')

    async def test_cancellation_releases_request(self):
        closed=[];request=SimpleNamespace(close=lambda:closed.append(True))
        native=SimpleNamespace(get_connection_parameters=lambda:SimpleNamespace(connection_interval=48,connection_latency=0))
        async def cancel(_):raise asyncio.CancelledError()
        with patch.object(wb,'make_windows_request',return_value=request),patch.object(wb.asyncio,'sleep',new=cancel):
            with self.assertRaises(asyncio.CancelledError):
                await wb.windows_throughput(SimpleNamespace(_backend=SimpleNamespace(_requester=native)),1,_platform='win32')
        self.assertEqual(closed,[True])

    async def test_connection_retains_then_releases_request_and_disconnects(self):
        closed=[];request=SimpleNamespace(close=lambda:closed.append('released'))
        native=SimpleNamespace(get_connection_parameters=lambda:SimpleNamespace(connection_interval=12,connection_latency=0))
        class Client(FakeClient):
            instances=[];magic=b'LCS5'
            def __init__(self,*a,**k):
                super().__init__(*a,**k);self._backend=SimpleNamespace(_requester=native)
        actual=wb.windows_throughput
        async def force_windows(client,device):return await actual(client,device,_platform='win32')
        with patch.dict(sys.modules,{'bleak':SimpleNamespace(BleakClient=Client)}),patch.object(wb,'make_windows_request',return_value=request),patch.object(wb,'windows_throughput',new=force_windows):
            async with wb.connection(1) as (client,_,__):
                self.assertEqual(closed,[]);self.assertTrue(client.is_connected)
            self.assertEqual(closed,['released']);self.assertFalse(client.is_connected)


if __name__=='__main__':unittest.main()
